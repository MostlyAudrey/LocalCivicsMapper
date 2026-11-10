"""Article processor: claim newest unparsed article -> Stage 1 -> Stage 2 -> Resolver -> PROCESSED.

The crawler only *queues* articles (PENDING). Processing is a set of independent "slots"; each slot
atomically claims the most recently published PENDING/STAGE1_DONE article (-> IN_PROGRESS), processes
it, and loops. Used by both `civic-tracker process` (drain the queue, then exit) and the long-running
worker (`civic-tracker worker`, never exits).

Per-article isolation: a failure marks only that article FAILED with the traceback in `error_log`.
A cancelled article (worker stop / CANCEL_PROCESSING) is released back to the queue, keeping any
Stage 1 output so it resumes at Stage 2.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, Optional

import psycopg
from psycopg_pool import AsyncConnectionPool

from civic_tracker.config import settings
from civic_tracker.db import repositories as repo
from civic_tracker.pipeline import telemetry
from civic_tracker.pipeline.encoder import Encoder, LocalEncoderClient
from civic_tracker.pipeline.jurisdiction import infer_jurisdiction
from civic_tracker.pipeline.resolver import resolve_article
from civic_tracker.pipeline.stage1_nlp import Stage1Processor
from civic_tracker.pipeline.stage2_llm import DeepSeekExtractor, Extractor, enforce_verbatim_quotes, sanitize_result

if TYPE_CHECKING:
    from civic_tracker.pipeline.triage import Triage

log = logging.getLogger(__name__)

LEAD_CHARS = 1000
RESOLVE_RETRIES = 4  # deadlock retries for the resolve transaction

# progress(slot, info) where info = {"article_id", "title", "stage", "since"} or None when the slot is idle
ProgressFn = Callable[[int, Optional[Dict]], None]


@dataclass
class RunStats:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    events_created: int = 0
    events_merged: int = 0
    failures: list = field(default_factory=list)
    log_lines: list = field(default_factory=list)

    def note(self, line: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.log_lines.append(f"{stamp} {line}")
        del self.log_lines[:-500]  # keep the tail


class Orchestrator:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        stage1: Optional[Stage1Processor] = None,
        extractor: Optional[Extractor] = None,
        encoder: Optional[Encoder] = None,
        concurrency: int = settings.LLM_MAX_CONCURRENCY,
        claim_oldest: bool = False,
        triage: Optional["Triage"] = None,
    ) -> None:
        """`triage` (pipeline/triage.py) screens each claimed article before any NLP/LLM work; None = off."""
        self.pool = pool
        self.claim_oldest = claim_oldest
        self.stage1 = stage1 or Stage1Processor()
        self.extractor = extractor or DeepSeekExtractor()
        self.encoder = encoder or LocalEncoderClient()
        self.concurrency = max(1, concurrency)
        self.triage = triage
        self._sources: Dict[int, tuple] = {}  # source_id -> (fetched_at, row); refreshed every 5 min

    async def _source(self, source_id: int) -> dict:
        hit = self._sources.get(source_id)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        async with self.pool.connection() as conn:
            row = await repo.get_source(conn, source_id) or {"name": "", "scrape_config": {}}
        self._sources[source_id] = (time.monotonic(), row)
        return row

    async def _triage(self, art: dict, stats: RunStats) -> bool:
        """True = keep processing. Runs once per article (triaged_at), unless an admin forced it ('override')."""
        if self.triage is None or art.get("triaged_at") is not None or art.get("skip_reason") == "override":
            return True
        res = await self.triage.check(url=art.get("url") or "", title=art.get("title") or "",
                                      text=art.get("full_text") or "", source=await self._source(art["source_id"]))
        async with self.pool.connection() as conn:
            await repo.save_triage(conn, art["id"], keep=res.keep, genre=res.genre, relevance=res.relevance,
                                   reason=res.reason)
        if not res.keep:
            stats.skipped += 1
            stats.note(f"SKIPPED #{art['id']} ({res.reason})"[:300] + f": {(art.get('title') or '')[:60]}")
        art["genre"] = res.genre  # Stage 2 + resolver use it (opinion rules, provisional events)
        return res.keep

    # ------------------------------------------------------------------
    async def claim_next(self) -> Optional[dict]:
        async with self.pool.connection() as conn:
            return await repo.claim_next_article(conn, oldest=self.claim_oldest)

    async def process_article(
        self, art: dict, stats: RunStats, progress: Optional[Callable[[str], None]] = None,
        replace_existing: bool = False,
    ) -> str:
        """Process one CLAIMED article. Returns 'PROCESSED' | 'SKIPPED' | 'FAILED'.
        replace_existing=True (re-extraction): the article's previous stance rows/event links are replaced
        atomically in the resolve transaction, and newly unsupported events/entities are deleted.
        Raises CancelledError after releasing the claim if cancelled."""
        aid = art["id"]
        title = (art.get("title") or "")[:80]
        step = progress or (lambda _stage: None)
        timings: Dict[str, Optional[int]] = {"stage1_ms": None, "llm_ms": None, "resolve_ms": None}
        t0 = time.monotonic()
        try:
            step("TRIAGE")
            if not await self._triage(art, stats):
                return "SKIPPED"
            bundle = art.get("evidence_bundle")
            candidates = list(art.get("candidate_entities") or [])

            if art.get("prev_status", "PENDING") == "PENDING" or not bundle:
                step("STAGE1")
                t0 = time.monotonic()
                s1 = await self.stage1.aprocess(art["full_text"])
                [art_emb] = await self.encoder.embed(
                    [f"{art['title']}\n{art['full_text'][:LEAD_CHARS]}"], task="ARTICLE_CLUSTER"
                )
                async with self.pool.connection() as conn:
                    async with conn.transaction():
                        await repo.save_stage1(
                            conn, aid,
                            resolved_text=s1.resolved_text,
                            evidence_bundle=s1.evidence_bundle,
                            candidate_entities=s1.candidate_entities,
                            # keep the claim while Stage 2 runs; a crash recovers to STAGE1_DONE
                            status="SKIPPED" if s1.skipped else "IN_PROGRESS",
                        )
                        await repo.set_article_embedding(conn, aid, art_emb)
                timings["stage1_ms"] = int((time.monotonic() - t0) * 1000)
                if s1.skipped:
                    await telemetry.record_stage_timings(self.pool, aid, **timings)
                    stats.skipped += 1
                    stats.note(f"SKIPPED #{aid} (no entities): {title}")
                    return "SKIPPED"
                bundle, candidates = s1.evidence_bundle, s1.candidate_entities

            step("STAGE2_LLM")

            async def on_call(timer, meta, status, error, _aid=aid, _chars=len(bundle or "")):
                await telemetry.record_llm_call(self.pool, kind="STAGE2_EXTRACT", timer=timer, meta=meta,
                                                status=status, error=error, article_id=_aid, input_chars=_chars)

            # which Yolo city the article is about: places bare "Planning Commission" etc. (prompt + resolver)
            src_cfg = (await self._source(art["source_id"])).get("scrape_config") or {} if art.get("source_id") else {}
            jurisdiction = infer_jurisdiction(art.get("title") or "", art.get("full_text") or "",
                                              src_cfg.get("jurisdiction"))
            # roll-call votes are recorded ONLY from official minutes (never from news)
            record_kind = (art.get("doc_meta") or {}).get("kind") if art.get("genre") == "official_record" else None
            t0 = time.monotonic()
            result = await self.extractor.extract(art["title"], art["published_at"], candidates, bundle,
                                                  on_call=on_call, genre=art.get("genre"), jurisdiction=jurisdiction,
                                                  record_kind=record_kind)
            timings["llm_ms"] = int((time.monotonic() - t0) * 1000)
            result, bad = sanitize_result(result)
            if bad:
                log.warning("article %s: dropped %d malformed affiliations/involvements", aid, bad)
            if not result.events:
                # Out-of-scope article (sports, arts, listings): don't harvest stray affiliations.
                result.affiliations = []
            result, dropped = enforce_verbatim_quotes(result, art["full_text"])
            if dropped:
                log.info("article %s: dropped %d non-verbatim quotes", aid, dropped)

            step("RESOLVE")
            t0 = time.monotonic()
            # Concurrent resolves (local slots + burst slots) touch shared TB_Entity/TB_Event rows and can
            # deadlock; Postgres aborts one side. The transaction is all-or-nothing, so just retry it.
            for attempt in range(RESOLVE_RETRIES):
                try:
                    async with self.pool.connection() as conn:
                        async with conn.transaction():
                            if replace_existing:
                                await repo.clear_article_extraction(conn, aid)
                            summary = await resolve_article(
                                conn, self.encoder, article_id=aid, published_at=art["published_at"],
                                result=result, genre=art.get("genre"), jurisdiction=jurisdiction,
                                allow_votes=record_kind == "minutes", text=art.get("full_text"),
                            )
                            if replace_existing:
                                gone = await repo.delete_orphans(conn)
                                if any(gone.values()):
                                    stats.note(f"#{aid}: removed {gone['events']} unsupported event(s), "
                                               f"{gone['entities']} orphaned entit(y/ies)")
                            await repo.set_article_status(conn, aid, "PROCESSED")
                    break
                except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
                    if attempt == RESOLVE_RETRIES - 1:
                        raise
                    log.warning("article %s: resolve deadlocked, retrying (%d/%d)", aid, attempt + 1, RESOLVE_RETRIES)
                    await asyncio.sleep(random.uniform(0.5, 2.0) * (attempt + 1))
            timings["resolve_ms"] = int((time.monotonic() - t0) * 1000)
            await telemetry.record_stage_timings(self.pool, aid, **timings)
            stats.processed += 1
            stats.events_created += summary.events_created
            stats.events_merged += summary.events_merged
            stats.note(f"OK #{aid}: {len(summary.entity_ids)} entities, {len(summary.event_ids)} events "
                       f"({summary.events_merged} merged): {title}")
            return "PROCESSED"
        except asyncio.CancelledError:
            try:
                async with self.pool.connection() as conn:
                    await repo.release_article(conn, aid)
            except Exception:  # pragma: no cover
                log.exception("could not release article %s", aid)
            stats.note(f"RELEASED #{aid} back to queue (cancelled): {title}")
            raise
        except Exception as exc:  # noqa: BLE001 - per-article isolation is the point
            log.exception("article %s failed", aid)
            stats.failed += 1
            stats.failures.append((aid, f"{type(exc).__name__}: {exc}"))
            stats.note(f"FAILED #{aid}: {type(exc).__name__}: {str(exc)[:200]} | {title}")
            try:
                async with self.pool.connection() as conn:
                    await repo.set_article_status(conn, aid, "FAILED", traceback.format_exc()[-8000:])
            except Exception:  # pragma: no cover
                log.exception("could not mark article %s FAILED", aid)
            return "FAILED"

    async def slot_loop(
        self,
        slot: int,
        stats: RunStats,
        *,
        stop_when_empty: bool = True,
        max_articles: Optional[int] = None,
        progress: Optional[ProgressFn] = None,
        on_empty: Optional[Callable[[], Awaitable[bool]]] = None,
        idle_sleep: float = 30.0,
        counter: Optional[list] = None,
        before_claim: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        """Claim -> process -> repeat. `on_empty` runs when the queue is empty (e.g. Stage 3 bios) and
        returns True if it did work (so we immediately look for articles again)."""
        report = progress or (lambda _s, _i: None)
        counter = counter if counter is not None else [0]
        while max_articles is None or counter[0] < max_articles:
            if before_claim is not None:
                await before_claim()  # e.g. blocks while the processor is paused
            # Reserve a place under max_articles BEFORE the (awaiting) claim; otherwise parallel slots sharing
            # `counter` all pass the check above and overshoot the limit.
            if max_articles is not None and counter[0] >= max_articles:
                break
            counter[0] += 1
            try:
                art = await self.claim_next()
            except BaseException:
                counter[0] -= 1
                raise
            if art is None:
                counter[0] -= 1
                report(slot, None)
                if stop_when_empty:
                    return
                if on_empty is not None and await on_empty():
                    continue
                await asyncio.sleep(idle_sleep)
                continue
            since = datetime.now(timezone.utc).isoformat()
            info = {"article_id": art["id"], "title": (art.get("title") or "")[:90], "stage": "CLAIMED", "since": since}
            report(slot, info)

            def step(stage: str, _info=info) -> None:
                _info["stage"] = stage
                report(slot, _info)

            await self.process_article(art, stats, step)
            report(slot, None)

    async def reextract(self, article_ids: list, stats: RunStats, *, should_stop: Optional[Callable[[], bool]] = None,
                        progress: Optional[ProgressFn] = None) -> None:
        """Re-run Stage 2 + resolver for already PROCESSED articles (e.g. after a prompt change), `concurrency`
        at a time. Articles are claimed directly (never re-queued); Stage 1 output is reused."""
        queue: asyncio.Queue = asyncio.Queue()
        for a in article_ids:
            queue.put_nowait(a)
        report = progress or (lambda _s, _i: None)

        async def slot(i: int) -> None:
            while not queue.empty() and not (should_stop and should_stop()):
                aid = queue.get_nowait()
                async with self.pool.connection() as conn:
                    art = await repo.claim_for_reextract(conn, aid)
                if art is None:
                    stats.note(f"#{aid}: not re-extractable (not PROCESSED or no Stage 1 output)")
                    continue
                info = {"article_id": aid, "title": (art.get("title") or "")[:90], "stage": "REEXTRACT",
                        "since": datetime.now(timezone.utc).isoformat()}
                report(i, info)

                def step(stage: str, _info=info) -> None:
                    _info["stage"] = stage
                    report(i, _info)

                await self.process_article(art, stats, step, replace_existing=True)
                report(i, None)

        await asyncio.gather(*(slot(i) for i in range(self.concurrency)))

    async def run(self, max_articles: Optional[int] = None) -> RunStats:
        """Drain the queue with `concurrency` slots, newest articles first, then return."""
        stats = RunStats()
        counter = [0]
        await asyncio.gather(*(
            self.slot_loop(i, stats, stop_when_empty=True, max_articles=max_articles, counter=counter)
            for i in range(self.concurrency)
        ))
        return stats
