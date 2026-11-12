"""Seats on a body ("District 3", "Trustee Area 4") belong to the affiliation, not to a separate entity (v2.1).

The extractor sometimes names a seat as if it were a body ("Riverton City Council District 2") or an office title
as if it were an organisation ("County Supervisor"). Left alone, each becomes its own entity and the real body's
roster falls apart. Before the resolver runs, `normalize_result`:

  * moves a seat written into the role ("Supervisor, District 3") to ExtractedAffiliation.seat;
  * rewrites "<body> District N" to the body + seat - only when <body> is already a known council/board, so real
    agencies ("Reclamation District 900") and unknown names are left untouched;
  * rewrites a bare office title ("County Supervisor") to the generic body it sits on ("Board of Supervisors"),
    which the resolver then places in the article's city or county (or lists as unresolved).
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from psycopg import AsyncConnection

log = logging.getLogger(__name__)

_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                 "eight": "8", "nine": "9", "ten": "10", "first": "1", "second": "2", "third": "3",
                 "fourth": "4", "fifth": "5", "sixth": "6", "seventh": "7"}
_KIND = r"(?:trustee\s+area|supervisorial\s+district|district|ward|area|seat|division)"
_NUM = r"(?:\d{1,3}|[a-h]|" + "|".join(_NUMBER_WORDS) + r")"
_ORDINAL = r"(?:\d{1,2}(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|seventh)"
_SEAT = re.compile(rf"\b(?P<kind>{_KIND})\s+(?:no\.?\s*|#\s*)?(?P<num>{_NUM})\b", re.I)
_SEAT_ORD = re.compile(rf"\b(?P<num>{_ORDINAL})\s+(?P<kind>district|ward)\b", re.I)
_SEAT_TAIL = re.compile(r"^\)?(?:\s+(?:office|seat))?$", re.I)   # what may follow the seat at the end of a name
# a body that has seats; "<this> District 3" is a seat of it, while "Reclamation District 900" is an agency
_SEATED_BODY = re.compile(r"\b(council|board|supervisors|trustees|commission|assembly|senate|house)\b", re.I)
# office title -> the generic body it sits on (the resolver places it in the article's city/county)
_OFFICE_BODY = [
    (re.compile(r"^(?:the\s+)?(?:county\s+)?supervisors?$", re.I), "Board of Supervisors"),
    (re.compile(r"^(?:the\s+)?(?:city\s+)?council\s*(?:member|man|woman|person)s?$", re.I), "City Council"),
]


def _kind_label(kind: str) -> str:
    k = " ".join(kind.lower().split())
    return {"supervisorial district": "District", "trustee area": "Area"}.get(k, k.title())


def _num_label(num: str) -> str:
    n = num.lower()
    n = re.sub(r"(st|nd|rd|th)$", "", n) if n[:1].isdigit() else n
    return _NUMBER_WORDS.get(n, n.upper() if len(n) == 1 and n.isalpha() else n)


def normalize_seat(text: Optional[str]) -> Optional[str]:
    """'district three' / '3rd District' / 'Supervisorial District 3' -> 'District 3'; 'trustee area 4' ->
    'Area 4'; anything that isn't a numbered seat -> None."""
    t = " ".join((text or "").split())
    m = _SEAT.search(t) or _SEAT_ORD.search(t)
    return f"{_kind_label(m.group('kind'))} {_num_label(m.group('num'))}" if m else None


def seat_from_role(role: str) -> Tuple[str, Optional[str]]:
    """'Supervisor, District 3' / 'District 3 Supervisor' -> ('Supervisor', 'District 3'); no seat -> (role, None).
    A role that is only a seat ('District 3') is kept as it is."""
    r = " ".join((role or "").split())
    m = _SEAT.search(r) or _SEAT_ORD.search(r)
    if not m:
        return r, None
    rest = (r[:m.start()] + " " + r[m.end():]).strip(" ,;:-()")
    rest = re.sub(r"\s*[,(]\s*\)?\s*$", "", re.sub(r"\s{2,}", " ", rest))
    rest = re.sub(r"\s+(?:for|of|in)$", "", rest, flags=re.I)
    return (rest, normalize_seat(m.group(0))) if rest else (r, None)


def split_seat(name: str) -> Optional[Tuple[str, str]]:
    """'Riverton City Council District 2' -> ('Riverton City Council', 'District 2'); also '..., District 2',
    '... District 2 Office' and '... District 2 Seat'. None when the name does not END in a seat, or when
    nothing is left of it ('District 3 Office' names no body)."""
    n = " ".join((name or "").split()).strip(" .,;:")
    for m in list(_SEAT.finditer(n)) + list(_SEAT_ORD.finditer(n)):
        if not _SEAT_TAIL.match(n[m.end():]):
            continue
        body = n[:m.start()].strip(" ,;:-(")
        return (body, normalize_seat(m.group(0))) if body else None
    return None


def office_body(name: str) -> Optional[str]:
    """'County Supervisor' -> 'Board of Supervisors'; 'City Council Member' -> 'City Council'; else None."""
    n = " ".join((name or "").split()).strip(" .,;:")
    for rx, body in _OFFICE_BODY:
        if rx.match(n):
            return body
    return None


def looks_like_seat_entity(name: str) -> bool:
    """For the admin audit: an entity name that is really a seat or an office title."""
    if office_body(name) or re.match(rf"^(?:the\s+)?{_SEAT.pattern}(?:\s+(?:office|seat))?$", name.strip(), re.I):
        return True
    s = split_seat(name)
    return bool(s and (_SEATED_BODY.search(s[0]) or re.search(r"\b(city|county|town)\b", s[0], re.I)))


async def _known_body(conn: AsyncConnection, name: str) -> Optional[str]:
    """The canonical spelling as written, when `name` is exactly a known body's name or alias."""
    cur = await conn.execute(
        """SELECT 1 FROM TB_Entity WHERE entity_type IN ('GOVERNMENT_BODY', 'ORGANIZATION')
             AND (LOWER(canonical_name) = LOWER(%(n)s) OR LOWER(%(n)s) = ANY(SELECT LOWER(unnest(aliases))))
           LIMIT 1""", {"n": name})
    return name if await cur.fetchone() else None


async def body_and_seat(conn: AsyncConnection, name: str, role: str = "") -> Optional[Tuple[str, str]]:
    """(body, seat) when `name` is a seat of a KNOWN body. '<County> District N' with a supervisor's role is a
    seat on that county's board; '<City> District N' with a council role is a seat on that city's council."""
    s = split_seat(name)
    if not s:
        return None
    body, seat = s
    tries: List[str] = [body] if _SEATED_BODY.search(body) else []
    if re.search(r"supervisor", role or "", re.I):
        tries.append(f"{body} Board of Supervisors")
    if re.search(r"council|mayor", role or "", re.I):
        tries.append(f"{re.sub(r'^city of ', '', body, flags=re.I)} City Council")
    for t in tries:
        if await _known_body(conn, t):
            return t, seat
    return None


async def normalize_result(conn: AsyncConnection, result) -> int:
    """Rewrite seat-as-body and title-as-body names in a Stage 2 result (in place). Returns how many it changed."""
    changed = 0
    for aff in result.affiliations:
        role, seat = seat_from_role(aff.role_title)
        aff.seat = normalize_seat(getattr(aff, "seat", None)) or seat
        if seat and role != aff.role_title:
            aff.role_title = role
            changed += 1
        org = aff.organization_name
        if not org:
            continue
        hit = await body_and_seat(conn, org, aff.role_title)
        if hit:
            aff.organization_name, aff.seat = hit[0], aff.seat or hit[1]
            changed += 1
        elif office_body(org):
            aff.organization_name = office_body(org)
            changed += 1
    for ev in result.events:
        for inv in ev.involvements:
            if inv.entity_type == "PERSON":
                continue
            hit = await body_and_seat(conn, inv.entity_name)
            if hit:
                inv.entity_name = hit[0]
                changed += 1
    if changed:
        log.info("seats: rewrote %d seat/office names to their body", changed)
    return changed


_READY = False


async def column_ready(conn: AsyncConnection) -> bool:
    """True once migration v2.1 is applied; until then seats are not stored (articles still process)."""
    global _READY
    if not _READY:
        cur = await conn.execute(
            """SELECT EXISTS (SELECT 1 FROM information_schema.columns
                              WHERE table_name = 'tb_entity_affiliation' AND column_name = 'seat') AS ok""")
        _READY = bool((await cur.fetchone())["ok"])
        if not _READY:
            log.warning("seats not stored: apply sql/unapplied/v2.1_AffiliationSeat.sql")
    return _READY
