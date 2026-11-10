"""Measures (ordinances, resolutions, contracts, budgets, projects...) and roll-call votes (v1.8).

* Roll calls are recorded ONLY from official minutes (user decision 2026-11-10); the orchestrator passes
  allow_votes=True for official_record articles whose doc_meta.kind is "minutes".
* Any article (news included) can LINK an event to a measure through ExtractedEvent.measures, so a measure page
  can list its news coverage. Opinion pieces may link to an existing measure but never create one.

Measure identity: normalized number within the jurisdiction ("Ordinance No. 2654" -> ORD-2654) when stated;
otherwise the same body + a similar title (trigram) or meaning (title embedding), else a new measure.
Member identity: minutes often give surnames only ("Ayes: Ashby, Chatham"), so a ballot name is matched to
people extracted from the same document, then to people affiliated with the voting body, then to an exact
person name; otherwise the ballot keeps only its raw name (never guessed).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence

from psycopg import AsyncConnection

from civic_tracker.db.repositories import to_vector_literal
from civic_tracker.pipeline.encoder import Encoder
from civic_tracker.pipeline.jurisdiction import is_generic_body, qualify_generic
from civic_tracker.pipeline.stage2_llm import ExtractedEvent, ExtractedMeasureRef, ExtractedVote

log = logging.getLogger(__name__)

MEASURE_TITLE_TRGM = 0.55
MEASURE_TITLE_COS = 0.88

# Similar-looking titles that are NOT the same measure (2026-11-10): "Appointment of Noel Ashford as
# President" vs "...Maureen Tull as Vice President"; "Minutes of December 13, 2020" vs "...April 21, 2021";
# "Consent calendar items 4 through 15" from five different meetings.
_MONTHS = ("january february march april may june july august september october november december "
           "jan feb mar apr jun jul aug sep sept oct nov dec").split()
# capitalised words that don't distinguish one item from another
_GENERIC_CAPS = set("""
a an and as at by for from in of on or the to with approval approve approving adopt adoption appointment
appoint election elect nomination nominate selection select reappointment item items agenda minutes meeting
meetings regular special council city county board commission committee subcommittee trustees trustee
president vice chair chairperson member members officer officers resolution ordinance no number consent
calendar public hearing report annual fiscal year fy budget amendment agreement contract plan program project
policy update review request authorization authorize
""".split())
# titles too generic to stand for one item across meetings: only the same meeting date may share them
_PER_MEETING = re.compile(r"\b(consent\s+calendar|minutes|agenda|meeting\s+schedule|election\s+of\s+officers|"
                          r"officers?|chair|vice[\s-]chair|president)\b", re.I)


def _numbers(title: str) -> set:
    toks = re.findall(r"[A-Za-z]+|\d+", title.lower())
    return {t.lstrip("0") or "0" for t in toks if t.isdigit()} | {t[:3] for t in toks if t in _MONTHS}


def _distinctive(title: str) -> set:
    return {w.lower() for w in re.findall(r"\b[A-Z][a-zA-Z'’-]+", title) if w.lower() not in _GENERIC_CAPS}


def titles_compatible(new: str, existing: str) -> bool:
    """Could two measure titles mean the same item? Not when their numbers/dates differ, or when EACH names
    something the other doesn't (different people, places, projects). One title merely being more specific
    ("Village Farms" vs "Village Farms Davis development") is fine."""
    na, nb = _numbers(new), _numbers(existing)
    if na and nb and na != nb:
        return False
    da, db = _distinctive(new), _distinctive(existing)
    low_a, low_b = new.lower(), existing.lower()
    only_a = {w for w in da - db if w not in low_b}
    only_b = {w for w in db - da if w not in low_a}
    return not (only_a and only_b)
_TYPE_PREFIX = {"ORDINANCE": "ORD", "RESOLUTION": "RES", "BALLOT_MEASURE": "MEASURE", "CONTRACT": "CONTRACT",
                "BUDGET": "BUDGET", "APPOINTMENT": "APPT", "LAND_USE": "LU", "POLICY": "POLICY", "OTHER": "ITEM"}
_NUM_WORDS = re.compile(r"\b(?:no\.?|number|#)\s*", re.I)
_MEMBER_TITLE = re.compile(  # longest forms first ("Mayor Pro Tem" before "Mayor")
    r"^(?:mayor\s+pro\s+tem(?:pore)?|vice\s+mayor|vice\s+chair(?:person)?|"
    r"(?:(?:vice|deputy)\s+)?(?:mayor|chair(?:person|woman|man)?|council\s*member|councilmember|councilman|"
    r"councilwoman|supervisor|commissioner|director|trustee|board\s*member|member|president))\.?\s+", re.I)


_READY: Optional[bool] = None


async def tables_ready(conn: AsyncConnection) -> bool:
    """True once migration v1.8 is applied. Until then measures/votes are skipped (the article still processes)."""
    global _READY
    if not _READY:  # re-check until present (the admin may apply the migration while the worker runs)
        cur = await conn.execute("SELECT to_regclass('tb_vote_motion') IS NOT NULL AS ok")
        _READY = bool((await cur.fetchone())["ok"])
        if not _READY:
            log.warning("measures/votes skipped: apply sql/unapplied/v1.8_MeasuresVotes.sql")
    return _READY


def number_key(ref: ExtractedMeasureRef) -> Optional[str]:
    """'Ordinance No. 2654' -> 'ORD-2654'; 'Resolution 26-031' -> 'RES-26-031'; 'Measure J' -> 'MEASURE-J'."""
    raw = (ref.measure_number or "").strip()
    if not raw:
        return None
    low = raw.lower()
    mtype = ref.measure_type
    for word, t in (("ordinance", "ORDINANCE"), ("resolution", "RESOLUTION"), ("measure", "BALLOT_MEASURE"),
                    ("ord.", "ORDINANCE"), ("res.", "RESOLUTION")):
        if word in low:
            mtype = t
            break
    core = _NUM_WORDS.sub("", re.sub(r"(?i)\b(ordinance|resolution|measure|ord\.|res\.)", "", raw)).strip(" .:-")
    core = re.sub(r"[^A-Za-z0-9-]+", "-", core).strip("-").upper()
    return f"{_TYPE_PREFIX.get(mtype, 'ITEM')}-{core}" if core else None


def clean_member_name(name: str) -> str:
    n = " ".join((name or "").replace("’", "'").split()).strip(" .,;:")
    return _MEMBER_TITLE.sub("", n).strip()


async def body_entity(conn: AsyncConnection, name: Optional[str], jurisdiction: Optional[str],
                      lookup: Callable[[str], Optional[int]]) -> Optional[int]:
    """Entity id of the acting body ('Davis City Council'), placing a bare name in the article's city."""
    if not name:
        return None
    if is_generic_body(name):
        name = qualify_generic(name, jurisdiction) or ""
        if not name:
            return None
    eid = lookup(name)
    if eid:
        return eid
    cur = await conn.execute(
        """SELECT id FROM TB_Entity WHERE entity_type IN ('GOVERNMENT_BODY', 'ORGANIZATION')
             AND (LOWER(canonical_name) = LOWER(%(n)s) OR LOWER(%(n)s) = ANY(SELECT LOWER(unnest(aliases))))
           ORDER BY (entity_type = 'GOVERNMENT_BODY') DESC, id LIMIT 1""", {"n": name})
    row = await cur.fetchone()
    return row["id"] if row else None


async def resolve_measure(
    conn: AsyncConnection, encoder: Encoder, ref: ExtractedMeasureRef, *, jurisdiction: Optional[str],
    body_id: Optional[int], action_date: Optional[date], tags: Sequence[str], create: bool = True,
) -> Optional[int]:
    key = number_key(ref)
    title = " ".join(ref.title.split())[:300]
    if not title and not key:
        return None
    row = None
    if key:
        cur = await conn.execute(
            "SELECT id FROM TB_Measure WHERE COALESCE(jurisdiction, '') = COALESCE(%s, '') AND number_key = %s",
            (jurisdiction, key))
        row = await cur.fetchone()
    [emb] = await encoder.embed([f"{ref.measure_type} {title}"], task="EVENT_DEDUP")
    if row is None:
        cur = await conn.execute(
            """SELECT id, title, last_action_date, first_action_date, similarity(title, %(t)s) AS trgm,
                      1 - (title_embedding <=> %(e)s::vector) AS cos
               FROM TB_Measure
               WHERE (body_entity_id IS NOT DISTINCT FROM %(b)s::int
                      OR (%(b)s::int IS NULL AND jurisdiction = %(j)s::text))
                 AND (number_key IS NULL OR %(k)s::text IS NULL OR number_key = %(k)s::text)
                 AND (last_action_date IS NULL OR %(d)s::date IS NULL
                      OR ABS(last_action_date - %(d)s::date) <= 1100)
                 AND (similarity(title, %(t)s) >= %(mt)s OR 1 - (title_embedding <=> %(e)s::vector) >= %(mc)s)
               ORDER BY GREATEST(similarity(title, %(t)s), 1 - (title_embedding <=> %(e)s::vector)) DESC
               LIMIT 5""",
            {"t": title, "e": to_vector_literal(emb), "b": body_id, "j": jurisdiction, "k": key,
             "d": action_date, "mt": MEASURE_TITLE_TRGM, "mc": MEASURE_TITLE_COS})
        per_meeting = bool(_PER_MEETING.search(title)) and not key
        for cand in await cur.fetchall():
            if not titles_compatible(title, cand["title"]):
                continue
            # "Consent calendar", "Approval of minutes", officer elections: one item per meeting
            if per_meeting and action_date and cand["last_action_date"] and \
                    action_date not in (cand["last_action_date"], cand["first_action_date"]):
                continue
            row = cand
            break
    if row is None:
        if not create:
            return None
        cur = await conn.execute(
            """INSERT INTO TB_Measure (body_entity_id, jurisdiction, measure_type, measure_number, number_key, title,
                                       first_action_date, last_action_date, topic_tags, title_embedding)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
               ON CONFLICT (COALESCE(jurisdiction, ''), number_key) WHERE number_key IS NOT NULL
               DO UPDATE SET updated_at = NOW()
               RETURNING id""",
            (body_id, jurisdiction, ref.measure_type, ref.measure_number, key, title, action_date, action_date,
             sorted(set(tags)), to_vector_literal(emb)))
        return (await cur.fetchone())["id"]
    await conn.execute(
        """UPDATE TB_Measure SET
               body_entity_id = COALESCE(body_entity_id, %(b)s),
               measure_number = COALESCE(measure_number, %(n)s), number_key = COALESCE(number_key, %(k)s),
               measure_type = CASE WHEN measure_type = 'OTHER' THEN %(ty)s ELSE measure_type END,
               first_action_date = LEAST(first_action_date, %(d)s), last_action_date = GREATEST(last_action_date, %(d)s),
               topic_tags = ARRAY(SELECT DISTINCT unnest(topic_tags || %(tags)s::text[])), updated_at = NOW()
           WHERE id = %(id)s""",
        {"b": body_id, "n": ref.measure_number, "k": key, "ty": ref.measure_type, "d": action_date,
         "tags": sorted(set(tags)), "id": row["id"]})
    return row["id"]


async def link_measure_event(conn: AsyncConnection, measure_id: int, event_id: int, how: str) -> None:
    await conn.execute(
        """INSERT INTO TB_Measure_Event (measure_id, event_id, how) VALUES (%s, %s, %s)
           ON CONFLICT (measure_id, event_id) DO UPDATE
             SET how = CASE WHEN EXCLUDED.how = 'vote' THEN 'vote' ELSE TB_Measure_Event.how END""",
        (measure_id, event_id, how))


async def match_member(conn: AsyncConnection, raw: str, body_id: Optional[int],
                       lookup: Callable[[str], Optional[int]]) -> Optional[int]:
    name = clean_member_name(raw)
    if not name:
        return None
    eid = lookup(name)
    if eid:
        return eid
    surname = name.split()[-1].lower()
    if body_id:
        cur = await conn.execute(
            """SELECT DISTINCT e.id, e.canonical_name FROM TB_Entity e
               JOIN TB_Entity_Affiliation a ON a.entity_id = e.id
               WHERE e.entity_type = 'PERSON' AND a.organization_id = %s""", (body_id,))
        members = await cur.fetchall()
        full = [m for m in members if m["canonical_name"].lower() == name.lower()]
        if len(full) == 1:
            return full[0]["id"]
        if len(name.split()) == 1:
            by_surname = [m for m in members if m["canonical_name"].split()[-1].lower() == surname]
            if len(by_surname) == 1:
                return by_surname[0]["id"]
    if len(name.split()) >= 2:
        cur = await conn.execute(
            """SELECT id FROM TB_Entity WHERE entity_type = 'PERSON'
                 AND (LOWER(canonical_name) = LOWER(%(n)s) OR LOWER(%(n)s) = ANY(SELECT LOWER(unnest(aliases))))
               LIMIT 2""", {"n": name})
        rows = await cur.fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
    return None


async def store_votes(
    conn: AsyncConnection, encoder: Encoder, *, article_id: int, event_id: int, event: ExtractedEvent,
    action_date: Optional[date], jurisdiction: Optional[str], lookup: Callable[[str], Optional[int]],
) -> int:
    """Record the event's roll calls (official minutes only - the caller checks). Returns motions stored."""
    n = 0
    for v in event.votes:
        body_id = await body_entity(conn, v.measure.body_name, jurisdiction, lookup)
        mid = await resolve_measure(conn, encoder, v.measure, jurisdiction=jurisdiction, body_id=body_id,
                                    action_date=action_date, tags=event.topic_tags)
        if mid is None:
            continue
        await link_measure_event(conn, mid, event_id, "vote")
        await conn.execute("UPDATE TB_Measure SET status = %s WHERE id = %s AND (last_action_date IS NULL "
                           "OR last_action_date <= %s::date OR %s::date IS NULL)",
                           (v.outcome, mid, action_date, action_date))
        cur = await conn.execute(
            """INSERT INTO TB_Vote_Motion (measure_id, event_id, body_entity_id, source_article_id, vote_date,
                                           motion_text, outcome, tally)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (source_article_id, measure_id, motion_text) DO UPDATE
                 SET outcome = EXCLUDED.outcome, tally = EXCLUDED.tally, event_id = EXCLUDED.event_id
               RETURNING id""",
            (mid, event_id, body_id, article_id, action_date, " ".join(v.motion.split())[:1000], v.outcome,
             v.tally))
        motion_id = (await cur.fetchone())["id"]
        await conn.execute("DELETE FROM TB_Vote WHERE motion_id = %s", (motion_id,))
        seen = set()
        for kind, names in (("AYE", v.ayes), ("NO", v.noes), ("ABSTAIN", v.abstain), ("ABSENT", v.absent),
                            ("RECUSED", v.recused)):
            for raw in names:
                raw = " ".join(raw.split())[:120]
                if not raw or raw.lower() in seen:
                    continue
                seen.add(raw.lower())
                member = await match_member(conn, raw, body_id, lookup)
                await conn.execute(
                    "INSERT INTO TB_Vote (motion_id, entity_id, raw_name, vote) VALUES (%s, %s, %s, %s)",
                    (motion_id, member, raw, kind))
        n += 1
    return n


async def link_event_measures(
    conn: AsyncConnection, encoder: Encoder, *, event_id: int, event: ExtractedEvent, action_date: Optional[date],
    jurisdiction: Optional[str], lookup: Callable[[str], Optional[int]], create: bool,
) -> int:
    """Link an event (from any article) to the measures it is about. Returns links made."""
    n = 0
    for ref in event.measures:
        body_id = await body_entity(conn, ref.body_name, jurisdiction, lookup)
        mid = await resolve_measure(conn, encoder, ref, jurisdiction=jurisdiction, body_id=body_id,
                                    action_date=action_date, tags=event.topic_tags, create=create)
        if mid is not None:
            await link_measure_event(conn, mid, event_id, "explicit")
            n += 1
    return n
