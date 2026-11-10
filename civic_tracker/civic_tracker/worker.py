"""Long-running pipeline worker (`civic-tracker worker`).

Replaces SPEC_WEB_UI's in-process FastAPI scheduler (we serve the web UI with Flask + gunicorn, see
CLAUDE.md). The worker keeps the Stage 1 models warm and runs three independent loops:

  * Crawler  - on the TB_Scheduler_State interval (or CRAWL_NOW / BACKFILL commands) crawls sources and
               only QUEUES articles (PENDING). One crawl at a time; all HTTP is polite (crawler/polite.py).
  * Processor - WORKER_SLOTS slots, each claiming the most recently published unparsed article,
               running Stage 1 -> 2 -> resolver, and looping. Paused via TB_Scheduler_State.processor_enabled.
               When the queue is empty, one slot writes Stage 3 bios for eligible dirty entities.
  * Control  - polls TB_Worker_Command (written by the admin panel) and publishes live status to
               TB_Worker_Status every 2 s (heartbeat, slot progress, crawl progress, next crawl).

Telemetry goes to TB_Job_Run (CRAWL / BACKFILL / PROCESS_PIPELINE / WIKI_SYNTHESIS); failed or partially
failed runs trigger the webhook alert. Long-lived processing runs are rotated hourly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from psycopg_pool import AsyncConnectionPool

from civic_tracker.config import settings
from civic_tracker.crawler.scraper import crawl_source, for_each_source
from civic_tracker.db import repositories as repo
from civic_tracker.pipeline.alerts import send_job_alert
from civic_tracker.pipeline.encoder import LocalEncoderClient
from civic_tracker.pipeline.orchestrator import Orchestrator, RunStats
from civic_tracker.pipeline.stage1_nlp import Stage1Processor
from civic_tracker.pipeline.stage2_llm import DeepSeekExtractor, TruncationFallbackExtractor
from civic_tracker.pipeline.stage3_wiki import WikiBioWriter, eligible_dirty_entities, synthesize_entity
from civic_tracker.pipeline.triage import Triage

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 2.0
COMMAND_POLL_SECONDS = 2.0
SCHEDULE_POLL_SECONDS = 15.0
RUN_ROTATE_SECONDS = 3600.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().strftime("%H:%M:%S")


def _final_status(ok: int, failed: int) -> str:
    if failed == 0:
        return "SUCCESS"
    return "PARTIAL_FAILURE" if ok > 0 else "FAILED"


class _Run:
    """An open TB_Job_Run row plus its in-memory counters/log."""

    def __init__(self, run_id: int, job_type: str) -> None:
        self.id = run_id
        self.job_type = job_type
        self.opened = time.monotonic()
        self.ok = 0
        self.failed = 0
        self.history_capped = False  # noted once when the queue cap pauses the history walk
        self.lines: List[str] = []

    def note(self, line: str) -> None:
        self.lines.append(f"{_stamp()} {line}")
        del self.lines[:-500]

    @property
    def log_text(self) -> str:
        return "\n".join(self.lines)


class Worker:
    def __init__(self, pool: AsyncConnectionPool, slots: int = settings.WORKER_SLOTS) -> None:
        self.pool = pool
        self.n_slots = max(1, slots)
        self.encoder = LocalEncoderClient()
        self.triage = Triage() if settings.TRIAGE_ENABLED else None  # shared by local + burst slots
        # local model; an output that hits max_tokens goes to the API model (daily cap) instead of a local retry
        fallback = DeepSeekExtractor.burst if settings.TRUNCATION_FALLBACK_TO_API and settings.BURST_LLM_API_KEY else None
        self.orch = Orchestrator(pool, stage1=Stage1Processor(),
                                 extractor=TruncationFallbackExtractor(DeepSeekExtractor(), fallback),
                                 encoder=self.encoder, concurrency=self.n_slots, triage=self.triage)
        self.bio_writer = WikiBioWriter()
        self.stop_event = asyncio.Event()
        self.started_at = _now()

        # processor
        self.stats = RunStats()
        self.slot_info: Dict[int, Optional[dict]] = {i: None for i in range(self.n_slots)}
        self.slot_tasks: List[asyncio.Task] = []
        self.processor_enabled = True
        self.proc_run: Optional[_Run] = None
        self.bio_run: Optional[_Run] = None
        self.bio_lock = asyncio.Lock()
        self.bio_active: Optional[str] = None
        self.last_bio_at = time.monotonic()  # first interleaved bio ~BIO_INTERLEAVE_MINUTES after start

        # crawler
        self.crawl_lock = asyncio.Lock()
        self.crawl_task: Optional[asyncio.Task] = None
        self.crawler_state = "IDLE"
        self.crawler_detail = ""
        self.crawl_active: Dict[str, str] = {}  # source name -> progress, while a crawl runs
        self.crawler_run_id: Optional[int] = None
        self.schedule = {"is_enabled": True, "interval_hours": 6.0, "processor_enabled": True}
        self._sched_jitter: tuple = (None, 0.0)  # (crawl it applies to, extra fraction of the interval)
        self.next_crawl_at: Optional[datetime] = None
        self.last_crawl_finished_at: Optional[datetime] = None
        self.last_history_round_at: Optional[datetime] = None  # history-only rounds between scheduled crawls
        self.background: set[asyncio.Task] = set()

        # API burst mode
        self.burst_task: Optional[asyncio.Task] = None
        self.burst_stop = False
        self.burst: Dict = {"active": False}
        self.burst_slots: Dict[int, Optional[dict]] = {}

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:  # pragma: no cover
                pass

        async with self.pool.connection() as conn:
            released = await repo.release_all_claims(conn)
            runs = await repo.fail_orphaned_runs(conn)
            cmds = await repo.fail_orphaned_commands(conn)
            self.last_crawl_finished_at = await repo.get_last_crawl_finished(conn)
            self.schedule = dict(await repo.get_scheduler_state(conn))
        self.processor_enabled = bool(self.schedule.get("processor_enabled", True))  # honour a pause from the start
        log.info("startup recovery: released %d claims, closed %d orphaned runs, %d commands", released, runs, cmds)

        # Warm the Stage 1 models in the BACKGROUND. Only article processing needs them; crawling, admin
        # commands and the heartbeat must not wait (on slow storage the torch/fastcoref imports alone can take
        # 10+ minutes). Slots that reach Stage 1 early simply wait on the model lock.
        self.warming = True

        async def warm() -> None:
            try:
                await asyncio.to_thread(self.orch.stage1.process, "Warm-up sentence about Mayor Jane Doe of Davis.")
                log.info("NLP models warm")
            except Exception:  # noqa: BLE001 - a broken model should surface on real articles, not block startup
                log.exception("model warm-up failed")
            finally:
                self.warming = False

        tasks = [
            asyncio.create_task(warm(), name="warm-up"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._command_loop(), name="commands"),
            asyncio.create_task(self._schedule_loop(), name="schedule"),
            asyncio.create_task(self._run_rotation_loop(), name="rotation"),
        ]
        self._start_slots()
        log.info("worker running with %d slots", self.n_slots)
        await self.stop_event.wait()

        log.info("shutting down")
        for t in tasks + self.slot_tasks + list(self.background) + ([self.crawl_task] if self.crawl_task else []):
            t.cancel()
        await asyncio.gather(*tasks, *self.slot_tasks, *self.background,
                             *([self.crawl_task] if self.crawl_task else []), return_exceptions=True)
        await self._close_proc_run()
        await self._close_bio_run()
        async with self.pool.connection() as conn:
            await repo.release_all_claims(conn)
        self.crawler_state, self.crawler_detail = "IDLE", "worker stopped"
        await self._write_status(processor_state="STOPPED")

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.background.add(t)
        t.add_done_callback(self.background.discard)
        return t

    # ------------------------------------------------------------------ status
    async def _write_status(self, processor_state: Optional[str] = None) -> None:
        busy = [i for i in self.slot_info.values() if i]
        if processor_state is None:
            if not self.processor_enabled:
                processor_state = "PAUSED"
            elif busy:
                processor_state = "RUNNING"
            elif self.bio_active:
                processor_state = "SYNTHESIZING"
            else:
                processor_state = "IDLE"
        detail = f"{len(busy)}/{self.n_slots} slots busy"
        if getattr(self, "warming", False):
            detail += " · loading NLP models (first articles wait for this)"
        if self.bio_active:
            detail += f"; writing bio: {self.bio_active}"
        status = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": self.started_at,
            "crawler_state": self.crawler_state,
            "crawler_detail": self.crawler_detail,
            "crawler_job_run_id": self.crawler_run_id,
            "processor_state": processor_state,
            "processor_detail": detail,
            "slots": [dict(info, slot=i) if info else {"slot": i} for i, info in self.slot_info.items()],
            "next_crawl_at": self.next_crawl_at if self.schedule.get("is_enabled") else None,
            "last_crawl_finished_at": self.last_crawl_finished_at,
            "burst": dict(self.burst, configured=bool(settings.BURST_LLM_API_KEY), model=settings.BURST_LLM_MODEL,
                          provider=settings.BURST_PROVIDER_NAME, default_cap_usd=settings.BURST_DEFAULT_SPEND_CAP_USD,
                          default_concurrency=settings.BURST_CONCURRENCY,
                          slots=[dict(v, slot=k) for k, v in self.burst_slots.items() if v]),
        }
        async with self.pool.connection() as conn:
            await repo.write_worker_status(conn, status)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self._write_status()
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")
            await asyncio.sleep(HEARTBEAT_SECONDS)

    # ------------------------------------------------------------------ processor
    def _start_slots(self) -> None:
        self.slot_tasks = [
            asyncio.create_task(
                self.orch.slot_loop(
                    i, self.stats, stop_when_empty=False, progress=self._slot_progress,
                    on_empty=self._on_queue_empty, idle_sleep=settings.WORKER_IDLE_SLEEP_SECONDS,
                    before_claim=self._processor_gate,
                ),
                name=f"slot-{i}",
            )
            for i in range(self.n_slots)
        ]

    async def _processor_gate(self) -> None:
        """Runs before every article claim: blocks while paused, and interleaves Stage 3 bios so they still
        get written when the queue never empties (one bio per BIO_INTERLEAVE_MINUTES, by whichever slot
        reaches this point first; the other slots keep processing articles)."""
        while not self.processor_enabled:
            await asyncio.sleep(5)
        interval = settings.BIO_INTERLEAVE_MINUTES * 60
        if interval <= 0 or time.monotonic() - self.last_bio_at < interval or self.bio_lock.locked():
            return
        async with self.bio_lock:
            if time.monotonic() - self.last_bio_at < interval:  # another slot just did one
                return
            self.last_bio_at = time.monotonic()
            async with self.pool.connection() as conn:
                ids = await eligible_dirty_entities(conn, 1)
            if ids:
                await self._write_bio(ids[0])
                await self._close_bio_run()  # one telemetry row per interleaved bio keeps the run log tidy

    def _slot_progress(self, slot: int, info: Optional[dict]) -> None:
        self.slot_info[slot] = dict(info) if info else None
        if info and self.proc_run is None:
            self._spawn(self._open_proc_run())

    async def _open_proc_run(self) -> None:
        if self.proc_run is not None:
            return
        self.proc_run = _Run(0, "PROCESS_PIPELINE")  # placeholder prevents double-open
        async with self.pool.connection() as conn:
            self.proc_run.id = await repo.job_run_start(conn, "PROCESS_PIPELINE")
        self.stats.log_lines.clear()
        self.stats.processed = self.stats.skipped = self.stats.failed = 0

    async def _close_proc_run(self) -> None:
        run = self.proc_run
        if run is None or not run.id:
            return
        self.proc_run = None
        ok = self.stats.processed + self.stats.skipped
        failed = self.stats.failed
        status = _final_status(ok, failed)
        summary = "; ".join(f"#{a}: {e}" for a, e in self.stats.failures[-5:]) or None
        async with self.pool.connection() as conn:
            await repo.job_run_finish(conn, run.id, status=status, processed=ok, failed=failed,
                                      log_output="\n".join(self.stats.log_lines), error_summary=summary)
        self.stats.processed = self.stats.skipped = self.stats.failed = 0
        self.stats.failures.clear()
        if status != "SUCCESS":
            await send_job_alert("PROCESS_PIPELINE", run.id, status, ok, failed, summary)

    async def _on_queue_empty(self) -> bool:
        """Queue drained: close the processing run, then write one Stage 3 bio (one slot at a time)."""
        if not any(self.slot_info.values()) and self.proc_run is not None:
            await self._close_proc_run()
        if self.bio_lock.locked() or not self.processor_enabled:
            return False
        async with self.bio_lock:
            async with self.pool.connection() as conn:
                ids = await eligible_dirty_entities(conn, 1)
            if not ids:
                await self._close_bio_run()
                return False
            await self._write_bio(ids[0])
            self.last_bio_at = time.monotonic()
            return True

    async def _write_bio(self, entity_id: int) -> None:
        if self.bio_run is None:
            async with self.pool.connection() as conn:
                self.bio_run = _Run(await repo.job_run_start(conn, "WIKI_SYNTHESIS"), "WIKI_SYNTHESIS")
        run = self.bio_run
        self.bio_active = f"entity {entity_id}"
        try:
            async with self.pool.connection() as conn:
                cur = await conn.execute("SELECT canonical_name FROM TB_Entity WHERE id = %s", (entity_id,))
                row = await cur.fetchone()
                name = row["canonical_name"] if row else str(entity_id)
                self.bio_active = name
                await synthesize_entity(conn, self.bio_writer, entity_id, pool=self.pool)
            run.ok += 1
            run.note(f"OK bio #{entity_id} {name}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("bio for entity %s failed", entity_id)
            run.failed += 1
            run.note(f"FAILED bio #{entity_id}: {type(exc).__name__}: {str(exc)[:200]}")
            # don't retry the same entity forever: clear the flag; next new article re-dirties it
            async with self.pool.connection() as conn:
                await conn.execute("UPDATE TB_Entity SET bio_is_dirty = FALSE WHERE id = %s", (entity_id,))
        finally:
            self.bio_active = None
        async with self.pool.connection() as conn:
            await repo.job_run_update(conn, run.id, processed=run.ok, failed=run.failed, log_output=run.log_text)

    async def _close_bio_run(self) -> None:
        run = self.bio_run
        if run is None:
            return
        self.bio_run = None
        status = _final_status(run.ok, run.failed)
        async with self.pool.connection() as conn:
            await repo.job_run_finish(conn, run.id, status=status, processed=run.ok, failed=run.failed,
                                      log_output=run.log_text)
        if status != "SUCCESS":
            await send_job_alert("WIKI_SYNTHESIS", run.id, status, run.ok, run.failed, run.lines[-1] if run.lines else None)

    async def _run_rotation_loop(self) -> None:
        """Keep the open PROCESS_PIPELINE run's counters fresh; rotate it hourly during long backlogs."""
        while True:
            await asyncio.sleep(30)
            try:
                run = self.proc_run
                if run is None or not run.id:
                    continue
                if time.monotonic() - run.opened > RUN_ROTATE_SECONDS:
                    await self._close_proc_run()
                    if any(self.slot_info.values()):
                        await self._open_proc_run()
                    continue
                async with self.pool.connection() as conn:
                    await repo.job_run_update(conn, run.id, processed=self.stats.processed + self.stats.skipped,
                                              failed=self.stats.failed, log_output="\n".join(self.stats.log_lines))
            except Exception:  # noqa: BLE001
                log.exception("run rotation failed")

    async def _cancel_processing(self) -> str:
        """Abort in-flight articles now (claims are released back to the queue) and pause the processor,
        otherwise the slots would immediately re-claim the same newest articles."""
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE TB_Scheduler_State SET processor_enabled = FALSE, updated_at = NOW() WHERE id = 1")
        self.processor_enabled = False
        n = sum(1 for i in self.slot_info.values() if i)
        for t in self.slot_tasks:
            t.cancel()
        await asyncio.gather(*self.slot_tasks, return_exceptions=True)
        self.slot_info = {i: None for i in range(self.n_slots)}
        await self._close_proc_run()
        self._start_slots()
        return f"aborted {n} in-flight article(s); processor paused"

    # ------------------------------------------------------------------ crawler
    async def _schedule_loop(self) -> None:
        while True:
            try:
                async with self.pool.connection() as conn:
                    self.schedule = dict(await repo.get_scheduler_state(conn))
                self.processor_enabled = bool(self.schedule.get("processor_enabled", True))
                interval = timedelta(hours=float(self.schedule.get("interval_hours") or 6.0))
                base = self.last_crawl_finished_at
                if base is not None and self._sched_jitter[0] != base:  # new random offset per finished crawl
                    self._sched_jitter = (base, random.uniform(0.0, settings.CRAWL_SCHEDULE_JITTER))
                extra = interval * self._sched_jitter[1] if base else timedelta(0)
                self.next_crawl_at = (base + interval + extra) if base else _now()
                due = self.schedule.get("is_enabled") and _now() >= self.next_crawl_at
                if due and not self.crawl_lock.locked():
                    self._start_crawl("CRAWL", source_id=None, backfill=False)
                elif (self.schedule.get("is_enabled") and settings.HISTORY_ENABLED and not self.crawl_lock.locked()
                      and (self.last_history_round_at is None or _now() - self.last_history_round_at
                           >= timedelta(minutes=settings.HISTORY_ROUND_GAP_MINUTES))):
                    async with self.pool.connection() as conn:
                        depth = await repo.queue_depth(conn)
                    if depth < settings.HISTORY_QUEUE_MAX:  # keep the processor fed with older articles
                        self.last_history_round_at = _now()
                        self._start_crawl("CRAWL", source_id=None, backfill=False, history_only=True)
            except Exception:  # noqa: BLE001
                log.exception("schedule loop failed")
            await asyncio.sleep(SCHEDULE_POLL_SECONDS)

    def _start_crawl(self, job_type: str, *, source_id: Optional[int], backfill: bool,
                     backfill_days: Optional[int] = None, since: Optional[str] = None,
                     limit: Optional[int] = None, command_id: Optional[int] = None,
                     history_only: bool = False) -> asyncio.Task:
        task = asyncio.create_task(self._crawl(job_type, source_id, backfill, backfill_days, since, limit, command_id,
                                               history_only))
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def _crawl(self, job_type: str, source_id: Optional[int], backfill: bool,
                     backfill_days: Optional[int], since: Optional[str], limit: Optional[int],
                     command_id: Optional[int], history_only: bool = False) -> None:
        from civic_tracker.crawler.scraper import window_from_args

        if self.crawl_lock.locked():
            self.crawler_detail = f"{job_type} queued behind the running crawl"
        async with self.crawl_lock:
            self.crawl_task = asyncio.current_task()
            self.crawler_state = "RUNNING"
            async with self.pool.connection() as conn:
                sources = await repo.list_sources(conn, source_id, active_only=source_id is None)
                run = _Run(await repo.job_run_start(conn, job_type, source_id), job_type)
            self.crawler_run_id = run.id
            status, result = "SUCCESS", ""
            try:
                start = window_from_args(backfill_days, since) if backfill else None
                total_new = history_new = 0
                # every regular crawl (scheduled or "Crawl now") also walks each source back in time, right
                # after its recent check, so a cancelled/restarted crawl still makes history progress
                do_history = job_type == "CRAWL" and settings.HISTORY_ENABLED

                async def one(s: dict) -> None:
                    nonlocal total_new, history_new
                    if history_only:
                        history_new += await self._history_one(s, run)
                        return
                    name = s["name"]
                    self._crawl_progress(name, "starting")
                    try:
                        st = await crawl_source(self.pool, s, since=start, backfill=backfill, limit=limit,
                                                progress=lambda msg: self._crawl_progress(name, msg))
                    except Exception as exc:  # noqa: BLE001 - one broken source must not stop the others
                        log.exception("crawl of %s failed", name)
                        run.failed += 1
                        run.note(f"{name}: FAILED {type(exc).__name__}: {str(exc)[:300]}")
                    else:
                        total_new += st.inserted
                        run.ok += st.inserted
                        run.failed += st.errors
                        run.note(f"{name} [{st.method}]: discovered {st.discovered}, new {st.new_urls}, "
                                 f"queued {st.inserted}, errors {st.errors}, requests {st.requests}, 429s {st.throttled}")
                        for e in st.error_samples:
                            run.note(f"  ! {e[:300]}")
                    finally:
                        self._crawl_progress(name, None)
                    async with self.pool.connection() as conn:
                        await repo.job_run_update(conn, run.id, processed=run.ok, failed=run.failed, log_output=run.log_text)
                    if do_history:
                        history_new += await self._history_one(s, run)

                if history_only:
                    run.note("history round (processing queue was running low)")
                await for_each_source(sources, one)
                status = "SUCCESS" if run.failed == 0 else ("PARTIAL_FAILURE" if run.ok or len(sources) > 1 else "FAILED")
                result = (f"history round: queued {history_new} older article(s) from {len(sources)} source(s)"
                          if history_only else
                          f"queued {total_new} new + {history_new} older article(s) from {len(sources)} source(s)")
            except asyncio.CancelledError:
                status, result = "CANCELLED", "stopped by admin"
                run.note("CANCELLED by admin")
            except Exception as exc:  # noqa: BLE001
                log.exception("crawl failed")
                status, result = "FAILED", f"{type(exc).__name__}: {exc}"
                run.note(result)
            finally:
                async with self.pool.connection() as conn:
                    await repo.job_run_finish(conn, run.id, status=status, processed=run.ok, failed=run.failed,
                                              log_output=run.log_text,
                                              error_summary=None if status in ("SUCCESS", "CANCELLED") else result)
                    if command_id:
                        await repo.finish_command(conn, command_id, "CANCELLED" if status == "CANCELLED" else
                                                  ("DONE" if status != "FAILED" else "FAILED"), result)
                if history_only:
                    self.last_history_round_at = _now()
                elif job_type == "CRAWL" and status != "CANCELLED":
                    self.last_crawl_finished_at = _now()
                self.crawl_active.clear()
                self.crawler_state, self.crawler_detail, self.crawler_run_id = "IDLE", result, None
                self.crawl_task = None
                if status in ("FAILED", "PARTIAL_FAILURE"):
                    await send_job_alert(job_type, run.id, status, run.ok, run.failed, result)

    async def _history_one(self, s: dict, run: "_Run") -> int:
        """Walk one source back in time by HISTORY_BATCH_PER_SOURCE articles (see crawl_source_history).
        Returns the number of older articles queued."""
        from datetime import date as _date

        from civic_tracker.crawler.scraper import crawl_source_history

        floor = _date.fromisoformat(settings.CRAWL_HISTORY_FLOOR)
        name = f"{s['name']} (history)"
        async with self.pool.connection() as conn:
            depth = await repo.queue_depth(conn)
        if depth >= settings.HISTORY_QUEUE_MAX:
            if not run.history_capped:
                run.history_capped = True
                run.note(f"history walk paused: {depth} articles already queued (cap {settings.HISTORY_QUEUE_MAX})")
            return 0
        try:
            st = await crawl_source_history(self.pool, s, floor=floor, batch=settings.HISTORY_BATCH_PER_SOURCE,
                                            progress=lambda msg: self._crawl_progress(name, msg))
        except Exception as exc:  # noqa: BLE001
            log.exception("history walk of %s failed", s["name"])
            run.failed += 1
            run.note(f"{name}: FAILED {type(exc).__name__}: {str(exc)[:300]}")
            st = None
        finally:
            self._crawl_progress(name, None)
        if st is None:
            return 0
        run.ok += st.inserted
        run.failed += st.errors
        run.note(f"{name} [{st.method}]: discovered {st.discovered}, queued {st.inserted}, "
                 f"out of window {st.out_of_window}, errors {st.errors}, requests {st.requests}, 429s {st.throttled}")
        for e in st.error_samples:
            run.note(f"  ! {e[:300]}")
        async with self.pool.connection() as conn:
            await repo.job_run_update(conn, run.id, processed=run.ok, failed=run.failed, log_output=run.log_text)
        return st.inserted

    def _crawl_progress(self, name: str, msg: Optional[str]) -> None:
        """Track per-source progress while several sources crawl at once; msg=None means that source finished."""
        if msg is None:
            self.crawl_active.pop(name, None)
        else:
            self.crawl_active[name] = msg
        items = list(self.crawl_active.items())
        if not items:
            return
        if len(items) == 1:
            self.crawler_detail = f"{items[0][0]}: {items[0][1]}"
        else:
            self.crawler_detail = f"{len(items)} sources at once · " + " | ".join(f"{n}: {m[:50]}" for n, m in items)

    # ------------------------------------------------------------------ commands
    async def _command_loop(self) -> None:
        while True:
            try:
                async with self.pool.connection() as conn:
                    cmd = await repo.next_command(conn)
                if cmd is None:
                    await asyncio.sleep(COMMAND_POLL_SECONDS)
                    continue
                await self._dispatch(cmd)
            except Exception:  # noqa: BLE001
                log.exception("command loop error")
                await asyncio.sleep(COMMAND_POLL_SECONDS)

    async def _finish(self, cmd_id: int, status: str, result: str) -> None:
        async with self.pool.connection() as conn:
            await repo.finish_command(conn, cmd_id, status, result)

    async def _dispatch(self, cmd: dict) -> None:
        c, p, cid = cmd["command"], cmd["params"] or {}, cmd["id"]
        log.info("command #%s %s %s", cid, c, p)
        if c == "CRAWL_NOW":
            self._start_crawl("CRAWL", source_id=p.get("source_id"), backfill=False, command_id=cid)
        elif c == "BACKFILL":
            if not p.get("source_id"):
                await self._finish(cid, "FAILED", "source_id is required")
                return
            self._start_crawl("BACKFILL", source_id=int(p["source_id"]), backfill=True,
                              backfill_days=p.get("backfill_days"), since=p.get("since"), limit=p.get("limit"),
                              command_id=cid)
        elif c == "STOP_CRAWL":
            task = self.crawl_task
            if task and not task.done():
                self.crawler_state = "STOPPING"
                task.cancel()
                await self._finish(cid, "DONE", "crawl cancelled")
            else:
                await self._finish(cid, "DONE", "no crawl was running")
        elif c == "CANCEL_PROCESSING":
            await self._finish(cid, "DONE", await self._cancel_processing())
        elif c == "BURST":
            if self.burst_task and not self.burst_task.done():
                await self._finish(cid, "FAILED", "a burst is already running")
            elif not settings.BURST_LLM_API_KEY:
                await self._finish(cid, "FAILED", "BURST_LLM_API_KEY is not set in civic_tracker/.env")
            else:
                self.burst_task = self._spawn(self._burst(
                    cid, limit=int(p.get("limit") or 50),
                    cap=float(p.get("max_spend_usd") or settings.BURST_DEFAULT_SPEND_CAP_USD),
                    concurrency=int(p.get("concurrency") or settings.BURST_CONCURRENCY)))
        elif c == "STOP_BURST":
            if self.burst_task and not self.burst_task.done():
                self.burst_stop = True
                self.burst["stopping"] = True
                await self._finish(cid, "DONE", "burst stopping after in-flight articles finish")
            else:
                await self._finish(cid, "DONE", "no burst was running")
        elif c == "SYNTHESIZE_BIOS":
            self._spawn(self._bios_now(cid, int(p.get("limit") or settings.BIO_BATCH_LIMIT)))
        elif c == "REGENERATE_BIO":
            self._spawn(self._bios_now(cid, 1, entity_id=int(p["entity_id"])))
        else:
            await self._finish(cid, "FAILED", f"unknown command {c}")

    async def _bios_now(self, cid: int, limit: int, entity_id: Optional[int] = None) -> None:
        async with self.bio_lock:
            async with self.pool.connection() as conn:
                ids = [entity_id] if entity_id else await eligible_dirty_entities(conn, limit)
            before_ok = self.bio_run.ok if self.bio_run else 0
            for eid in ids:
                await self._write_bio(eid)
            ok = (self.bio_run.ok if self.bio_run else 0) - before_ok
            await self._close_bio_run()
        await self._finish(cid, "DONE", f"wrote {ok}/{len(ids)} bio(s)")


class _BurstDone(Exception):
    pass


async def _burst_impl(self: "Worker", cid: int, *, limit: int, cap: float, concurrency: int) -> None:
    """API burst: `concurrency` extra slots on the paid endpoint, claiming newest-first exactly like the local
    processor, until `limit` articles, the `cap` spend, the queue, or STOP_BURST.

    The two never collide: a claim is one atomic UPDATE ... FOR UPDATE SKIP LOCKED that flips the article
    to IN_PROGRESS, so an article being worked on by either process is invisible to the other.
    In-flight requests are allowed to finish, so the final spend can exceed the cap by at most a few
    articles' worth (fractions of a cent each at DeepSeek prices)."""
    extractor = DeepSeekExtractor.burst()
    orch = Orchestrator(self.pool, stage1=self.orch.stage1, extractor=extractor, encoder=self.encoder,
                        concurrency=concurrency, claim_oldest=False, triage=self.triage)
    stats, counter = RunStats(), [0]
    self.burst_stop = False
    async with self.pool.connection() as conn:
        run_id = await repo.job_run_start(conn, "API_BURST")
    self.burst = {"active": True, "stopping": False, "limit": limit, "cap_usd": cap, "concurrency": concurrency,
                  "spent_usd": 0.0, "done": 0, "failed": 0, "claimed": 0, "started_at": _now().isoformat(),
                  "job_run_id": run_id, "stop_reason": None}
    reason = {"why": "queue empty"}

    async def gate() -> None:
        self.burst.update(spent_usd=round(extractor.spent_usd, 4), done=stats.processed + stats.skipped,
                          failed=stats.failed, claimed=counter[0])
        if self.burst_stop:
            reason["why"] = "stopped by admin"
            raise _BurstDone
        if extractor.spent_usd >= cap:
            reason["why"] = f"spend cap ${cap:.2f} reached"
            raise _BurstDone
        if counter[0] >= limit:
            reason["why"] = f"article limit {limit} reached"
            raise _BurstDone

    def progress(slot: int, info: Optional[dict]) -> None:
        self.burst_slots[slot] = dict(info) if info else None

    async def slot(i: int) -> None:
        try:
            await orch.slot_loop(i, stats, stop_when_empty=True, max_articles=limit, progress=progress,
                                 before_claim=gate, counter=counter)
        except _BurstDone:
            pass
        finally:
            self.burst_slots.pop(i, None)

    status, err = "SUCCESS", None
    try:
        await asyncio.gather(*(slot(i) for i in range(concurrency)))
        if reason["why"] == "queue empty" and counter[0] >= limit:
            reason["why"] = f"article limit {limit} reached"  # slot_loop stops on the limit before the gate runs
    except asyncio.CancelledError:
        status, reason["why"] = "CANCELLED", "worker shutting down"
        raise
    except Exception as exc:  # noqa: BLE001
        status, err = "FAILED", f"{type(exc).__name__}: {exc}"
        log.exception("burst failed")
    finally:
        ok, failed = stats.processed + stats.skipped, stats.failed
        if status == "SUCCESS" and failed:
            status = _final_status(ok, failed)
        summary = (f"{ok} article(s) via {extractor.provider} ({extractor.model}), {failed} failed, "
                   f"${extractor.spent_usd:.4f} spent; stopped: {reason['why']}")
        stats.note(summary)
        async with self.pool.connection() as conn:
            await repo.job_run_finish(conn, run_id, status=status, processed=ok, failed=failed,
                                      log_output="\n".join(stats.log_lines), error_summary=err)
            await repo.finish_command(conn, cid, "FAILED" if status == "FAILED" else
                                      ("CANCELLED" if status == "CANCELLED" else "DONE"), err or summary)
        self.burst = {"active": False, "last_result": summary, "last_finished_at": _now().isoformat()}
        self.burst_slots.clear()
        if status in ("FAILED", "PARTIAL_FAILURE"):
            await send_job_alert("API_BURST", run_id, status, ok, failed, err or summary)


Worker._burst = _burst_impl  # type: ignore[attr-defined]


async def run_worker(slots: int = settings.WORKER_SLOTS) -> None:
    from civic_tracker.db.connection import close_pool, get_pool

    pool = await get_pool()
    try:
        await Worker(pool, slots).run()
    finally:
        await close_pool()
