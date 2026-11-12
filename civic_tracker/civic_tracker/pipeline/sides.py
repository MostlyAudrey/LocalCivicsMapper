"""Event sides (v2.0, PLAN_STANCE_SIDES.md).

A multi-sided event (an election, a lawsuit, a controversy, a split vote) lists its sides, and each stance row says
which side(s) its entity is on. The coalition matrix then pairs people by side instead of by stance polarity, so
two people who each SUPPORT their own candidate are opponents, and a PAC that OPPOSES candidate B is on candidate
A's side. An entity can be on more than one side (an endorser of two candidates in a multi-seat race).

Sources: Stage 2 extraction (new articles), roll calls with dissent (aye / no), and the assign-sides backfill
(existing events, API model). A 'manual' side is an admin decision and survives re-extraction.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Sequence

from psycopg import AsyncConnection

log = logging.getLogger(__name__)

SIDE_KINDS = ("candidate", "position", "party", "vote", "other")
_READY: Optional[bool] = None
_FILLER = re.compile(r"\b(the|campaign|camp|side|supporters?|backers?|team)\b")


def side_key(label: str) -> str:
    """Normalized label so wording variants of one side merge: 'Jane Roe campaign' = 'Jane Roe' = 'jane roe's
    supporters'. 'For Measure J' and 'Against Measure J' stay apart."""
    s = (label or "").lower().replace("’", "'")
    s = re.sub(r"'s\b", "", s)
    s = _FILLER.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def keyed(labels: Iterable[str]) -> Dict[str, str]:
    """{side_key: first label} for a list of labels (blank keys dropped)."""
    out: Dict[str, str] = {}
    for lb in labels:
        k = side_key(lb)
        if k and k not in out:
            out[k] = lb.strip()
    return out


async def tables_ready(conn: AsyncConnection) -> bool:
    """True once migration v2.0 is applied; until then sides are skipped (articles still process)."""
    global _READY
    if not _READY:
        cur = await conn.execute("SELECT to_regclass('tb_entity_event_side') IS NOT NULL AS ok")
        _READY = bool((await cur.fetchone())["ok"])
        if not _READY:
            log.warning("event sides skipped: apply sql/unapplied/v2.0_EventSides.sql")
    return _READY


async def ensure_side(conn: AsyncConnection, event_id: int, label: str, kind: str = "other",
                      source: str = "extraction") -> Optional[int]:
    key = side_key(label)
    if not key:
        return None
    kind = kind if kind in SIDE_KINDS else "other"
    cur = await conn.execute(
        """INSERT INTO TB_Event_Side (event_id, label, label_key, side_kind, source)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (event_id, label_key) DO UPDATE SET label = TB_Event_Side.label
           RETURNING id""",
        (event_id, label.strip()[:160], key, kind, source))
    return (await cur.fetchone())["id"]


async def set_entity_sides(conn: AsyncConnection, *, event_id: int, entity_id: int, side_ids: Sequence[int],
                           article_id: Optional[int] = None, replace_sources: Sequence[str] = ()) -> int:
    """Put the entity's stance rows in this event (one article's, or all when article_id is None) on these sides.
    `replace_sources`: first drop the rows' links to sides of these sources (re-extraction / re-backfill).
    Returns the number of stance rows linked."""
    cur = await conn.execute(
        "SELECT id FROM TB_Entity_Event WHERE event_id = %s AND entity_id = %s"
        + (" AND source_article_id = %s" if article_id is not None else ""),
        (event_id, entity_id, article_id) if article_id is not None else (event_id, entity_id))
    row_ids = [r["id"] for r in await cur.fetchall()]
    if not row_ids:
        return 0
    if replace_sources:
        await conn.execute(
            """DELETE FROM TB_Entity_Event_Side l USING TB_Event_Side s
               WHERE s.id = l.side_id AND l.entity_event_id = ANY(%s) AND s.source = ANY(%s)""",
            (row_ids, list(replace_sources)))
    for rid in row_ids:
        for sid in side_ids:
            await conn.execute(
                "INSERT INTO TB_Entity_Event_Side (entity_event_id, side_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (rid, sid))
    return len(row_ids)


async def drop_unused(conn: AsyncConnection, event_ids: Optional[List[int]] = None) -> int:
    """Remove non-manual sides nobody is on any more (after re-extraction or merges)."""
    cur = await conn.execute(
        """DELETE FROM TB_Event_Side s
           WHERE s.source <> 'manual' AND NOT EXISTS (SELECT 1 FROM TB_Entity_Event_Side l WHERE l.side_id = s.id)
             AND (%(ids)s::int[] IS NULL OR s.event_id = ANY(%(ids)s::int[]))""",
        {"ids": event_ids})
    return cur.rowcount or 0


async def store_extracted_sides(conn: AsyncConnection, *, event_id: int, article_id: int, sides,
                                memberships: Dict[int, List[str]]) -> int:
    """Persist one extracted event's sides. `sides`: [ExtractedSide]; `memberships`: {entity_id: [labels]}.
    The extraction considered sides either way, so the event is marked checked (the backfill skips it)."""
    if not await tables_ready(conn):
        return 0
    await conn.execute("UPDATE TB_Event SET sides_checked_at = NOW() WHERE id = %s", (event_id,))
    if not sides:
        return 0
    ids: Dict[str, int] = {}
    for s in sides:
        sid = await ensure_side(conn, event_id, s.label, s.kind, "extraction")
        if sid:
            ids[side_key(s.label)] = sid
    n = 0
    for entity_id, labels in memberships.items():
        sids = [ids[k] for k in keyed(labels) if k in ids]
        n += bool(await set_entity_sides(conn, event_id=event_id, entity_id=entity_id, side_ids=sids,
                                         article_id=article_id, replace_sources=("extraction",)))
    return n


async def store_rollcall_sides(conn: AsyncConnection, *, event_id: int, motion: str,
                               ballots: Dict[int, str]) -> int:
    """A split roll call (someone voted NO): members who voted AYE vs. NO on the motion are on opposite sides.
    `ballots`: {entity_id: 'AYE' | 'NO' | ...}. Only members with a stance row in the event are linked."""
    if "NO" not in ballots.values() or "AYE" not in ballots.values() or not await tables_ready(conn):
        return 0
    short = " ".join(motion.split())[:100]
    aye = await ensure_side(conn, event_id, f"Voted yes: {short}", "vote", "roll_call")
    no = await ensure_side(conn, event_id, f"Voted no: {short}", "vote", "roll_call")
    n = 0
    for entity_id, vote in ballots.items():
        if vote in ("AYE", "NO"):
            n += bool(await set_entity_sides(conn, event_id=event_id, entity_id=entity_id,
                                             side_ids=[aye if vote == "AYE" else no]))
    return n


async def rollcall_sides_for_event(conn: AsyncConnection, *, event_id: int, article_id: int) -> int:
    """Roll-call sides for every split motion this article recorded on the event (run after its stance rows)."""
    if not await tables_ready(conn):
        return 0
    cur = await conn.execute(
        """SELECT m.motion_text, v.entity_id, v.vote FROM TB_Vote_Motion m JOIN TB_Vote v ON v.motion_id = m.id
           WHERE m.event_id = %s AND m.source_article_id = %s AND v.entity_id IS NOT NULL ORDER BY m.id""",
        (event_id, article_id))
    motions: Dict[str, Dict[int, str]] = {}
    for r in await cur.fetchall():
        motions.setdefault(r["motion_text"], {})[r["entity_id"]] = r["vote"]
    n = 0
    for motion, ballots in motions.items():
        n += await store_rollcall_sides(conn, event_id=event_id, motion=motion, ballots=ballots)
    return n
