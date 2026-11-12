"""Backfill event sides for events extracted before v2.0 (PLAN_STANCE_SIDES.md, step 4).

For each event with two or more stance-taking entities that no LLM has checked for sides yet, one short prompt
(title, summary, the actors with their roles, stances and actions - not the article) asks for the competing sides
and who is on each. Only TB_Event_Side / TB_Entity_Event_Side are written; stances, quotes and summaries are
untouched. Runs on the burst API model (user decision 2026-11-12) under a per-run spend cap.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from openai import AsyncOpenAI
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from civic_tracker.config import settings
from civic_tracker.pipeline import sides as S
from civic_tracker.pipeline.telemetry import llm_call_timer, record_llm_call, usage_fields

log = logging.getLogger(__name__)

# contested kinds first (elections, lawsuits, controversies, votes), newest first within each
PRIORITY = ("ELECTION", "LEGAL_ACTION", "PUBLIC_CONTROVERSY", "LEGISLATIVE_VOTE")
MAX_ACTORS = 40


class SideOut(BaseModel):
    label: str = Field(max_length=80)
    kind: str = "other"


class MemberOut(BaseModel):
    id: int
    sides: List[str] = Field(default_factory=list)


class SidesAnswer(BaseModel):
    contested: bool
    sides: List[SideOut] = Field(default_factory=list)
    members: List[MemberOut] = Field(default_factory=list)


SYSTEM = """You label the competing SIDES of a local civic event from a list of its actors. A stance says HOW an \
actor acted (support, oppose, defend, apologize...); a side says WHOSE side it is on.

Rules:
- Elections: each candidate (or each position on a ballot measure) is a side, kind "candidate" or "position". The \
candidate, their campaign staff, endorsers, donors and PACs that help them are on that side. A group that OPPOSES \
candidate B in a two-way race is on candidate A's side; with more candidates, put it on no side unless the actions \
say whom it backs. Someone endorsing two candidates for two seats is on both sides.
- Lawsuits: plaintiffs vs. defendants. Controversies: critics vs. those defending the conduct. Split votes: those \
for vs. against.
- Reporters, moderators, election officials, neutral experts and bystanders are on no side.
- If the event is not contested (an announcement, a unanimous decision, a meeting), answer contested=false with \
no sides.
- 2 to 6 sides, short labels ("Jane Roe campaign", "For Measure J", "Plaintiffs", "Critics of the stop").
- members: one entry per actor that is on a side, by its numeric id, with the side labels exactly as listed.

Reply with ONE json object only: {"contested": bool, "sides": [{"label": str, "kind": \
"candidate"|"position"|"party"|"other"}], "members": [{"id": int, "sides": [str]}]}"""

CANDIDATES_SQL = """
    SELECT ev.id, ev.canonical_title, ev.event_type, ev.event_date, ev.summary
    FROM TB_Event ev
    WHERE ev.sides_checked_at IS NULL
      AND (%(event_id)s::int IS NULL OR ev.id = %(event_id)s)
      AND (SELECT count(DISTINCT ee.entity_id) FROM TB_Entity_Event ee
           WHERE ee.event_id = ev.id AND NOT ee.disputed AND ee.stance NOT IN ('NEUTRAL', 'PARTICIPANT')) >= 2
    ORDER BY array_position(%(prio)s::text[], ev.event_type) NULLS LAST, ev.event_date DESC NULLS LAST, ev.id DESC
    LIMIT %(limit)s
"""

ACTORS_SQL = """
    SELECT e.id, e.canonical_name, e.entity_type,
           array_agg(DISTINCT ee.role_in_event) AS roles, array_agg(DISTINCT ee.stance) AS stances,
           (array_agg(ee.action_summary ORDER BY ee.confidence DESC, ee.id))[1:2] AS actions
    FROM TB_Entity_Event ee JOIN TB_Entity e ON e.id = ee.entity_id
    WHERE ee.event_id = %s AND NOT ee.disputed
    GROUP BY e.id, e.canonical_name, e.entity_type
    ORDER BY bool_or(ee.stance NOT IN ('NEUTRAL', 'PARTICIPANT')) DESC, e.id
    LIMIT %s
"""


def build_prompt(event: dict, actors: List[dict]) -> str:
    lines = [f"Event: {event['canonical_title']}", f"Type: {event['event_type']}",
             f"Date: {event['event_date'] or 'unknown'}", f"Summary: {event['summary']}", "", "Actors:"]
    for a in actors:
        acts = " | ".join((x or "")[:220] for x in (a["actions"] or []) if x)
        lines.append(f"[{a['id']}] {a['canonical_name']} ({a['entity_type']}); role: {'; '.join(a['roles'][:3])}; "
                     f"stance: {', '.join(a['stances'])}; did: {acts}")
    return "\n".join(lines)


def parse_answer(content: str, actor_ids: set) -> SidesAnswer:
    raw = content[content.find("{"): content.rfind("}") + 1]
    ans = SidesAnswer.model_validate(json.loads(raw))
    listed = {S.side_key(s.label) for s in ans.sides}
    ans.members = [MemberOut(id=m.id, sides=[x for x in m.sides if S.side_key(x) in listed])
                   for m in ans.members if m.id in actor_ids]
    ans.members = [m for m in ans.members if m.sides]
    if not ans.contested or len(ans.sides) < 2 or not ans.members:
        return SidesAnswer(contested=False)
    return ans


@dataclass
class BackfillStats:
    checked: int = 0
    contested: int = 0
    linked: int = 0
    failed: int = 0
    spent_usd: float = 0.0
    stopped_at_cap: bool = False
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.checked} events checked, {self.contested} contested, {self.linked} actors placed on sides, "
                f"{self.failed} failed, ${self.spent_usd:.3f}" + (" (stopped at spend cap)" if self.stopped_at_cap else ""))


class SidesBackfill:
    def __init__(self, pool: AsyncConnectionPool, *, cap_usd: Optional[float] = None,
                 concurrency: Optional[int] = None) -> None:
        if not settings.BURST_LLM_API_KEY:
            raise RuntimeError("BURST_LLM_API_KEY is not set in civic_tracker/.env")
        self.pool = pool
        self.cap = settings.SIDES_BACKFILL_SPEND_CAP_USD if cap_usd is None else cap_usd
        self.sem = asyncio.Semaphore(concurrency or settings.BURST_CONCURRENCY)
        self.client = AsyncOpenAI(base_url=settings.BURST_LLM_BASE_URL, api_key=settings.BURST_LLM_API_KEY,
                                  timeout=settings.LLM_TIMEOUT_SECONDS, max_retries=2)
        self.extra_body = json.loads(settings.BURST_EXTRA_BODY or "{}") or None
        self.stats = BackfillStats()
        self.stop = False

    def _cost(self, meta: dict) -> float:
        return ((meta.get("prompt_tokens") or 0) * settings.BURST_PRICE_INPUT_PER_M
                + (meta.get("completion_tokens") or 0) * settings.BURST_PRICE_OUTPUT_PER_M) / 1_000_000

    async def _ask(self, event: dict, actors: List[dict]) -> SidesAnswer:
        prompt = build_prompt(event, actors)
        meta = {"model": settings.BURST_LLM_MODEL, "provider": settings.BURST_PROVIDER_NAME,
                "thinking": settings.BURST_THINKING}
        status, error, content = "OK", None, None
        try:
            # the timer sets duration_ms on exit, so the call is recorded after the `with` block
            with llm_call_timer(settings.BURST_PROVIDER_NAME) as timer:
                resp = await self.client.chat.completions.create(
                    model=settings.BURST_LLM_MODEL,
                    messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=settings.SIDES_BACKFILL_MAX_TOKENS,
                    response_format={"type": "json_object"}, extra_body=self.extra_body)
            meta.update(usage_fields(resp))
            meta["cost_usd"] = self._cost(meta)
            self.stats.spent_usd += meta["cost_usd"]
            content = resp.choices[0].message.content or ""
            return parse_answer(content, {a["id"] for a in actors})
        except Exception as exc:  # noqa: BLE001 - recorded, the event stays unchecked for the next run
            status, error = "ERROR", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await record_llm_call(self.pool, kind="ASSIGN_SIDES", timer=timer, meta=meta, status=status,
                                  input_chars=len(prompt), error=error)

    async def _one(self, event: dict) -> None:
        async with self.sem:
            if self.stop or self.stats.spent_usd >= self.cap:
                self.stats.stopped_at_cap = self.stats.stopped_at_cap or self.stats.spent_usd >= self.cap
                return
            async with self.pool.connection() as conn:
                actors = await (await conn.execute(ACTORS_SQL, (event["id"], MAX_ACTORS))).fetchall()
            try:
                ans = await self._ask(event, actors)
            except Exception as exc:  # noqa: BLE001
                self.stats.failed += 1
                self.stats.errors.append(f"event {event['id']}: {exc}"[:300])
                log.warning("assign-sides event %s failed: %s", event["id"], exc)
                return
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    await save_answer(conn, event["id"], ans)
            self.stats.checked += 1
            if ans.contested:
                self.stats.contested += 1
                self.stats.linked += len(ans.members)

    async def run(self, limit: int = 200, event_id: Optional[int] = None) -> BackfillStats:
        async with self.pool.connection() as conn:
            if not await S.tables_ready(conn):
                raise RuntimeError("apply sql/unapplied/v2.0_EventSides.sql first")
            events = await (await conn.execute(
                CANDIDATES_SQL, {"event_id": event_id, "prio": list(PRIORITY), "limit": limit})).fetchall()
        log.info("assign-sides: %d events to check (cap $%.2f)", len(events), self.cap)
        await asyncio.gather(*(self._one(ev) for ev in events))
        log.info("assign-sides: %s", self.stats.summary())
        return self.stats


async def save_answer(conn, event_id: int, ans: SidesAnswer) -> None:
    """Replace the event's backfilled sides with this answer and mark it checked."""
    await conn.execute(
        """DELETE FROM TB_Entity_Event_Side l USING TB_Event_Side s
           WHERE s.id = l.side_id AND s.event_id = %s AND s.source = 'backfill'""", (event_id,))
    await conn.execute("DELETE FROM TB_Event_Side WHERE event_id = %s AND source = 'backfill'", (event_id,))
    ids: Dict[str, int] = {}
    for s in ans.sides if ans.contested else []:
        sid = await S.ensure_side(conn, event_id, s.label, s.kind, "backfill")
        if sid:
            ids[S.side_key(s.label)] = sid
    for m in ans.members if ans.contested else []:
        sids = [ids[k] for k in S.keyed(m.sides) if k in ids]
        if sids:
            await S.set_entity_sides(conn, event_id=event_id, entity_id=m.id, side_ids=sids)
    await conn.execute("UPDATE TB_Event SET sides_checked_at = NOW() WHERE id = %s", (event_id,))
