"""Seat names ("District 3", "Trustee Area 4") - the website's copy of the pure rules in
civic_tracker/pipeline/seats.py (different venv, so mirrored: keep the two in sync).

A seat is a field of an affiliation (TB_Entity_Affiliation.seat, v2.1), never an entity of its own."""
from __future__ import annotations

import re
from typing import Optional, Tuple

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
_SEATED_BODY = re.compile(r"\b(council|board|supervisors|trustees|commission|assembly|senate|house|congressional)\b", re.I)
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
