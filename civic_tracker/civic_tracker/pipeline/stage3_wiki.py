"""Stage 3: incremental Wikipedia-style lead bios (TB_Entity.summary_bio). SPEC_WEB_UI.md §4.

Only entities flagged dirty by the resolver (bio_is_dirty) AND with enough material get a bio:
  >= BIO_MIN_EVENTS events, or >= BIO_MIN_EVENTS_WITH_AFFILIATION events plus >= 1 affiliation.
The rest of each wiki page is rendered straight from SQL; the LLM only writes this 2-3 paragraph lead,
fed by the entity's top-20 salience-ranked events (topic_rank <= 5), roles, and coalition partners.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

from openai import AsyncOpenAI
from psycopg import AsyncConnection
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from civic_tracker.config import settings
from civic_tracker.pipeline.telemetry import llm_call_timer, usage_fields

log = logging.getLogger(__name__)

BIO_MIN_EVENTS = 3
BIO_MIN_EVENTS_WITH_AFFILIATION = 2
# Coalition rules, mirrored in the website's lib/db_wiki.py; keep the two in sync:
# an ally needs MIN_ALLY_SHARED_EVENTS shared events (one shared event is often one article), and any
# ally/opponent pair needs one reputable article (reliability >= REPUTABLE_MIN_RELIABILITY, not opinion,
# neither row claimed/disputed) that puts both in the same event; and the other entity must have at least
# MIN_COUNTERPART_EVENTS events overall and show up beyond one case file (an affiliation, a non-court/police
# event, or 2+ sources) - a one-off defendant in a DA release is not an "opponent".
MIN_ALLY_SHARED_EVENTS = 2
REPUTABLE_MIN_RELIABILITY = 0.7
MIN_COUNTERPART_EVENTS = 2
REPUTABLE_LINK_SQL = """
  AND (SELECT count(DISTINCT z.event_id) FROM TB_Entity_Event z
       WHERE z.entity_id = m.entity_b_id AND NOT z.disputed) >= %(min_counterpart)s
  AND (EXISTS (SELECT 1 FROM TB_Entity_Affiliation af WHERE af.entity_id = m.entity_b_id)
       OR EXISTS (SELECT 1 FROM TB_Entity_Event z JOIN TB_Event ze ON ze.id = z.event_id
                  WHERE z.entity_id = m.entity_b_id AND NOT z.disputed
                    AND ze.event_type NOT IN ('LEGAL_ACTION', 'LAW_ENFORCEMENT_ACTION'))
       OR (SELECT count(DISTINCT za.source_id) FROM TB_Entity_Event z
           JOIN TB_Article za ON za.id = z.source_article_id
           WHERE z.entity_id = m.entity_b_id AND NOT z.disputed) >= 2)
  AND EXISTS (
      SELECT 1
      FROM TB_Entity_Event x
      JOIN TB_Entity_Event y ON y.event_id = x.event_id AND y.source_article_id = x.source_article_id
      JOIN TB_Article a ON a.id = x.source_article_id
      JOIN TB_Sources s ON s.id = a.source_id
      WHERE x.entity_id = m.entity_a_id AND y.entity_id = m.entity_b_id
        AND NOT x.disputed AND NOT y.disputed
        AND s.reliability >= %(min_rel)s
        AND COALESCE(a.genre, 'unknown') <> 'opinion'
        AND COALESCE(x.evidence_type, 'reported') <> 'claimed'
        AND COALESCE(y.evidence_type, 'reported') <> 'claimed')
"""


class WikiBioSynthesis(BaseModel):
    lead_markdown: str = Field(
        max_length=4000,
        description=(
            "2 to 3 concise, objective, encyclopedic paragraphs summarizing this entity's "
            "career roles, highest-salience policy actions, major controversies, and "
            "recurring coalition partners or opponents. Weight coverage toward events "
            "with the highest article citation counts and salience scores. "
            "Wrap any mentioned canonical person, government body, or organization name "
            "in double brackets like [[Gloria Ravel]] or [[Davis Police Department]] "
            "for automatic internal wiki linking."
        ),
    )


SYSTEM_PROMPT = """You write the lead section of an internal civic wiki, in the neutral, encyclopedic voice \
of Wikipedia. You are given structured facts extracted from local news coverage about ONE entity.

Rules:
- Use ONLY the facts provided. Never add outside knowledge, speculation, or characterizations not supported \
by the facts. If coverage is thin, write less.
- 2 to 3 short paragraphs. First sentence: who/what the entity is (name, primary role/organization).
- Allocate space in proportion to Salience and article citations: the highest-salience events get the most \
coverage; low-salience routine items may be omitted.
- Describe stances neutrally ("apologized for", "defended", "opposed", "called for a review of"). Attribute \
claims ("according to", "alleged") when the facts are allegations.
- Facts come ONLY from the "Most salient events" list. Items under "Commentary" are opinions or claims by \
others: never state them as fact or as the entity's own position. Mention commentary at most briefly and \
always attributed (e.g. "Critics writing in the Davisite argued ..."), and only if it is a recurring theme.
- Wrap names of other people, government bodies and organizations that appear in the facts in double \
brackets, e.g. [[Davis Police Department]]. Do not bracket the entity itself.
- Plain prose paragraphs separated by a blank line. No headings, lists, or quotes longer than one sentence.
Respond with JSON matching the schema only."""

ELIGIBLE_DIRTY_SQL = """
WITH stats AS (
    SELECT e.id,
           (SELECT COUNT(DISTINCT ee.event_id) FROM TB_Entity_Event ee WHERE ee.entity_id = e.id) AS n_events,
           (SELECT COUNT(*) FROM TB_Entity_Affiliation a WHERE a.entity_id = e.id) AS n_affs
    FROM TB_Entity e
    WHERE e.bio_is_dirty = TRUE
)
SELECT id, n_events, n_affs FROM stats
WHERE n_events >= %(min_events)s OR (n_events >= %(min_events_aff)s AND n_affs >= 1)
ORDER BY n_events DESC, id ASC
LIMIT %(limit)s
"""

TOP_SALIENT_SQL = """
WITH top_salient AS (
    SELECT event_date, event_title, event_type, topic_tags, entity_article_citations, total_event_articles,
           primary_role_in_event, primary_stance, combined_action_summary, all_direct_quotes, salience_score,
           source_names, has_commentary
    FROM VW_Entity_Salient_Career_Events
    WHERE entity_id = %(entity_id)s
      AND topic_rank <= 5
      AND NOT commentary_only          -- v1.5: facts need adequately weighted evidence
    ORDER BY salience_score DESC, event_date DESC NULLS LAST
    LIMIT 20
)
SELECT * FROM top_salient
ORDER BY event_date ASC NULLS LAST, salience_score DESC
"""

# Low-weight material (op-eds, letters, third-party claims): given to the writer separately, attributed.
COMMENTARY_SQL = """
SELECT w.action_summary, w.stance, w.source_name, w.genre, w.evidence_type, w.claimed_by, ev.canonical_title,
       w.published_at
FROM VW_Entity_Event_Weighted w JOIN TB_Event ev ON ev.id = w.event_id
WHERE w.entity_id = %(entity_id)s AND NOT w.disputed AND w.weight < 0.2
ORDER BY w.published_at DESC NULLS LAST
LIMIT 8
"""


async def eligible_dirty_entities(conn: AsyncConnection, limit: int = 100) -> List[int]:
    cur = await conn.execute(
        ELIGIBLE_DIRTY_SQL,
        {"min_events": BIO_MIN_EVENTS, "min_events_aff": BIO_MIN_EVENTS_WITH_AFFILIATION, "limit": limit},
    )
    return [r["id"] for r in await cur.fetchall()]


async def build_context(conn: AsyncConnection, entity_id: int) -> Optional[str]:
    cur = await conn.execute("SELECT id, canonical_name, entity_type, aliases FROM TB_Entity WHERE id = %s", (entity_id,))
    ent = await cur.fetchone()
    if not ent:
        return None
    cur = await conn.execute(
        """
        SELECT a.role_title, o.canonical_name AS org, a.first_seen_date, a.last_seen_date
        FROM TB_Entity_Affiliation a LEFT JOIN TB_Entity o ON o.id = a.organization_id
        WHERE a.entity_id = %s ORDER BY a.first_seen_date, a.last_seen_date
        """,
        (entity_id,),
    )
    roles = await cur.fetchall()
    cur = await conn.execute(TOP_SALIENT_SQL, {"entity_id": entity_id})
    events = await cur.fetchall()
    params = {"eid": entity_id, "min_rel": REPUTABLE_MIN_RELIABILITY, "min_shared": MIN_ALLY_SHARED_EVENTS,
              "min_counterpart": MIN_COUNTERPART_EVENTS}
    cur = await conn.execute(
        """SELECT m.entity_b_name, m.shared_events_count, m.alignment_index FROM VW_Entity_Coalition_Matrix m
           WHERE m.entity_a_id = %(eid)s AND m.alignment_index > 0
             AND m.shared_events_count >= %(min_shared)s""" + REPUTABLE_LINK_SQL + """
           ORDER BY m.shared_events_count DESC, m.alignment_index DESC LIMIT 5""",
        params,
    )
    allies = await cur.fetchall()
    cur = await conn.execute(
        """SELECT m.entity_b_name, m.opposing_stance_count, m.alignment_index FROM VW_Entity_Coalition_Matrix m
           WHERE m.entity_a_id = %(eid)s AND m.alignment_index < 0""" + REPUTABLE_LINK_SQL + """
           ORDER BY m.opposing_stance_count DESC LIMIT 5""",
        params,
    )
    opponents = await cur.fetchall()

    def d(x) -> str:
        return x.strftime("%Y-%m-%d") if x else "undated"

    lines = [f"Entity: {ent['canonical_name']} ({ent['entity_type']})"]
    if ent["aliases"]:
        lines.append("Also known as: " + ", ".join(ent["aliases"]))
    lines.append("\nCareer roles:")
    lines += [f"- {r['role_title']}" + (f", {r['org']}" if r["org"] else "")
              + f" (seen {d(r['first_seen_date'])} to {d(r['last_seen_date'])})" for r in roles] or ["- (none recorded)"]
    lines.append("\nMost salient events (chronological; supported by news reporting or the entity's own statements):")
    for e in events:
        quotes = "; ".join(f'"{q}"' for q in (e["all_direct_quotes"] or [])[:2])
        lines.append(
            f"[{d(e['event_date'])}] {e['event_title']} (Salience: {e['salience_score']} | Cited in "
            f"{e['entity_article_citations']}/{e['total_event_articles']} articles | Sources: "
            f"{', '.join(e.get('source_names') or [])} | Topics: "
            f"{', '.join(e['topic_tags'] or [])}) — Role: {e['primary_role_in_event']} [{e['primary_stance']}]: "
            f"{e['combined_action_summary']}" + (f" Quotes: {quotes}" if quotes else "")
        )
    cur = await conn.execute(COMMENTARY_SQL, {"entity_id": entity_id})
    commentary = await cur.fetchall()
    if commentary:
        lines.append("\nCommentary about this entity (opinion pieces / third-party claims - NOT established facts; "
                     "mention only with attribution, or omit):")
        for c in commentary:
            who = c["claimed_by"] or f"a {c['genre'] or 'commentary'} piece"
            lines.append(f"- In {c['source_name']}, {who} ({d(c['published_at'])}, re: {c['canonical_title']}): "
                         f"{c['action_summary']}")
    if allies:
        lines.append("\nFrequent allies (shared events, alignment): "
                     + "; ".join(f"{a['entity_b_name']} ({a['shared_events_count']}, {a['alignment_index']:+.2f})" for a in allies))
    if opponents:
        lines.append("Frequent counterparts/opponents (opposing events, alignment): "
                     + "; ".join(f"{o['entity_b_name']} ({o['opposing_stance_count']}, {o['alignment_index']:+.2f})" for o in opponents))
    return "\n".join(lines)


_LEAK = re.compile(r"\b(?:actually|wait|the instructions|i will|i'll|let me|hmm)\b", re.IGNORECASE)


def looks_clean(text: str) -> bool:
    """Reject output where the model reasoned inside the JSON string (thinking off)."""
    return bool(text.strip()) and not _LEAK.search(text) and text.count("?") <= 1


class WikiBioWriter:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY,
                                  timeout=settings.LLM_TIMEOUT_SECONDS, max_retries=1)
        self.schema = WikiBioSynthesis.model_json_schema()

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, max=20), reraise=True)
    async def _call(self, context: str, thinking: bool, on_call=None) -> str:
        meta: dict = {"model": settings.LLM_MODEL, "thinking": thinking}
        status, error = "OK", None
        try:
            with llm_call_timer() as timer:
                try:
                    return await self._request(context, thinking, meta)
                except asyncio.CancelledError:
                    status, error = "CANCELLED", "cancelled"
                    raise
                except Exception as exc:
                    status = "TRUNCATED" if meta.get("finish_reason") == "length" else "ERROR"
                    error = f"{type(exc).__name__}: {exc}"
                    raise
        finally:
            if on_call is not None:
                try:
                    await on_call(timer, meta, status, error)
                except Exception:  # noqa: BLE001
                    log.exception("bio telemetry callback failed")

    async def _request(self, context: str, thinking: bool, meta: dict) -> str:
        resp = await self.client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context}],
            temperature=0.2,
            max_tokens=settings.LLM_MAX_TOKENS if thinking else 2000,
            response_format={"type": "json_schema",
                             "json_schema": {"name": "WikiBioSynthesis", "schema": self.schema, "strict": True}},
            extra_body={"chat_template_kwargs": {"thinking": thinking}},
        )
        from civic_tracker.pipeline.stage2_llm import _THINK_RE
        import json

        meta.update(usage_fields(resp))
        content = _THINK_RE.sub("", resp.choices[0].message.content or "").strip()
        return WikiBioSynthesis.model_validate(json.loads(content[content.find("{"): content.rfind("}") + 1])).lead_markdown.strip()

    async def write(self, context: str, on_call=None) -> str:
        """Thinking OFF (fast, per user decision); fall back once to thinking ON if the prose leaks reasoning."""
        text = await self._call(context, False, on_call)
        if looks_clean(text):
            return text
        log.info("bio output looked like leaked reasoning; retrying with thinking on")
        return await self._call(context, True, on_call)


async def synthesize_entity(conn: AsyncConnection, writer: WikiBioWriter, entity_id: int, pool=None) -> bool:
    """`pool` (optional) enables TB_LLM_Call telemetry for the bio request(s)."""
    context = await build_context(conn, entity_id)
    if context is None:
        return False
    callback = None
    if pool is not None:
        from civic_tracker.pipeline.telemetry import record_llm_call

        async def _record(timer, meta, status, error):
            await record_llm_call(pool, kind="STAGE3_BIO", timer=timer, meta=meta, status=status, error=error,
                                  entity_id=entity_id, input_chars=len(context))
        callback = _record
    lead = await writer.write(context, callback)
    await conn.execute(
        "UPDATE TB_Entity SET summary_bio = %s, bio_is_dirty = FALSE, bio_updated_at = NOW() WHERE id = %s",
        (lead, entity_id),
    )
    return True
