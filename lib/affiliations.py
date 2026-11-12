"""Condense an entity's affiliation rows for display.

The extractor records the same position under many spellings ("Commissioner", "Commission Member",
"Commissioner, Winters Climate Action Commission", "Climate Action Commission Member"), sometimes without the
organization. For the wiki we show one line per (organization, kind of role) with the full date range:

  Winters Climate Action Commission
    Commissioner   May 2022 - Sep 2026   (also recorded as: Commission Member, Member)
    Vice Chair     Oct 2022 - Jul 2025

Rows are only regrouped for display; TB_Entity_Affiliation is unchanged.
"""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional, Tuple

Row = Dict[str, Any]

# (pattern on the cleaned, lowercased role, family key, label; None label = use the org kind)
_FAMILIES: List[Tuple[str, str, Optional[str]]] = [
    (r"\bvice[\s-]*chair", "vice-chair", "Vice Chair"),
    (r"\bco[\s-]*chair", "co-chair", "Co-Chair"),
    (r"\bchair(man|woman|person)?\b", "chair", "Chair"),
    (r"\b(mayor\s+pro\s+tem(pore)?|pro\s+tem(pore)?|vice\s+mayor)\b", "mayor-pro-tem", "Mayor Pro Tem"),
    (r"^(the\s+)?mayor\b", "mayor", "Mayor"),
    (r"\b(city\s+)?council\s*(member|man|woman)\b|\bcouncilmember\b", "councilmember", "Councilmember"),
    (r"\bsupervisor\b", "supervisor", "Supervisor"),
    (r"\btrustee\b", "trustee", "Trustee"),
    (r"\bsecretary\b", "secretary", "Secretary"),
    (r"\btreasurer\b", "treasurer", "Treasurer"),
    (r"\bliaison\b", "liaison", "Liaison"),
    (r"\bpresident\b", "president", None),  # keep "Vice President" vs "President" apart below
    (r"\b(commissioner|commission\s+member|committee\s+member|board\s+member|volunteer\s+member|member)\b",
     "member", None),
]
_STOP = {"the", "of", "and", "city", "county", "member", "commissioner", "a"}
_DISTRICT_FAMILIES = {"supervisor", "councilmember", "trustee", "member"}   # roles elected to a numbered seat
_MEMBER_LABELS = ("Commissioner", "Board Member", "Trustee", "Member", "Committee Member")


def _clean_role(role: str, org: Optional[str]) -> str:
    r = (role or "").strip()
    r = r.split(",")[0]                       # "Commissioner, Winters Climate Action Commission"
    r = re.sub(r"\s+(of|for|on|with)\s+.*$", "", r, flags=re.I)   # "City Clerk of Woodland"
    if org:
        r = re.sub(re.escape(org), "", r, flags=re.I)
    return " ".join(r.split())


_INCOMING = re.compile(r"\b(elect|designate|incoming)\b", re.I)   # won or was named, not yet in the seat


def role_family(role: str, org: Optional[str]) -> Tuple[str, str]:
    """(family key, display label) for a role title. "Supervisor-Elect" is its own family ("supervisor-elect"):
    the person does not hold the seat yet."""
    key, label = _role_family(role, org)
    if _INCOMING.search(role or "") and not key.endswith("-elect"):
        return f"{key}-elect", f"{_INCOMING.sub('', label).strip(' -')}-Elect"
    return key, label


def _role_family(role: str, org: Optional[str]) -> Tuple[str, str]:
    r = _clean_role(role, org)
    low = r.lower()
    for rx, key, label in _FAMILIES:
        if re.search(rx, low):
            if key == "president":
                key = "vice-president" if "vice" in low else "president"
                return key, "Vice President" if key == "vice-president" else "President"
            if label is None:  # a plain member: name it after the body
                o = (org or role or "").lower()
                label = ("Commissioner" if "commission" in o else "Board Member" if "board" in o
                         else "Trustee" if "trustees" in o else "Member")
            return key, label
    return low or "role", r or role


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if w not in _STOP}


def _same_body(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b) and " ".join(a.lower().split()) == " ".join(b.lower().split())


def condense(rows: List[Row]) -> List[Row]:
    """Rows of (role_title, organization_id, organization, first_seen_date, last_seen_date) ->
    [{organization_id, organization, last_seen, roles: [{label, first_seen, last_seen, variants}]}],
    most recently active organization first."""
    orgs: "OrderedDict[str, Row]" = OrderedDict()   # body key -> {organization_id, organization}
    for r in rows:
        if r.get("organization"):
            key = " ".join(r["organization"].lower().split())  # duplicate entities with the same name: one body
            orgs.setdefault(key, {"organization_id": r["organization_id"], "organization": r["organization"]})

    fam_rows: Counter = Counter()  # (body, family) -> rows, to place org-less rows
    fam_span: Counter = Counter()  # (body, family) -> days between first and last sighting
    for x in rows:
        if x.get("organization"):
            k = (" ".join(x["organization"].lower().split()), role_family(x["role_title"], x["organization"])[0])
            fam_rows[k] += 1
            if x.get("first_seen_date") and x.get("last_seen_date"):
                fam_span[k] += (x["last_seen_date"] - x["first_seen_date"]).days

    def place(r: Row) -> Optional[str]:
        """Which body a row belongs to. A row on the whole city ("Councilmember, City of Winters") goes to the
        city's body where the person held that role (Winters City Council). An org-less row goes to the body its
        wording names best ("Climate Action Commission Member" -> Winters Climate Action Commission, not
        ...Committee), else to the body where the person held that kind of role most (then longest)."""
        if r.get("organization"):
            key = " ".join(r["organization"].lower().split())
            m = re.fullmatch(r"(?:the\s+)?(?:city|town|county)\s+of\s+(.+)", key)
            if m:
                fam = role_family(r.get("role_title", ""), r["organization"])[0]
                bodies = {b for (b, f) in fam_rows if f == fam and b != key and m.group(1) in b}
                if len(bodies) == 1:
                    return bodies.pop()
            return key
        title = (r.get("role_title") or "").lower()
        exact = [k for k in orgs if k in title]
        if len(exact) == 1:
            return exact[0]
        words = _words(title)
        scored = sorted(((len(_words(o["organization"]) & words) / max(1, len(_words(o["organization"]))), k)
                         for k, o in orgs.items()), reverse=True)
        if scored and scored[0][0] >= 0.5 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        fam = role_family(r.get("role_title", ""), None)[0]
        held = sorted(((n, fam_span[(body, f)], body) for (body, f), n in fam_rows.items() if f == fam),
                      reverse=True)
        if held and (len(held) == 1 or held[0][:2] != held[1][:2]):
            return held[0][2]
        return None

    groups: "OrderedDict[Tuple[Optional[str], str], Row]" = OrderedDict()
    for r in rows:
        body = place(r)
        org_name = orgs[body]["organization"] if body else None
        fam, label = role_family(r.get("role_title", ""), org_name)
        g = groups.setdefault((body, fam), {"label": label, "first_seen": r.get("first_seen_date"),
                                             "last_seen": r.get("last_seen_date"), "titles": Counter(),
                                             "seat": None, "seat_verified": False})
        if r.get("seat") and (r.get("seat_verified_at") or not g["seat_verified"]):
            g["seat"], g["seat_verified"] = r["seat"], bool(r.get("seat_verified_at"))
        g["titles"][(r.get("role_title") or "").strip()] += 1
        if r.get("first_seen_date") and (g["first_seen"] is None or r["first_seen_date"] < g["first_seen"]):
            g["first_seen"] = r["first_seen_date"]
        if r.get("last_seen_date") and (g["last_seen"] is None or r["last_seen_date"] > g["last_seen"]):
            g["last_seen"] = r["last_seen_date"]

    # a seat known from one row of a body ("Supervisor-Elect, District 3") is the person's seat in their other
    # seat-holding roles there ("Supervisor"); a verified seat wins. The seat someone is a candidate for is
    # not carried over: a sitting member can run for a different one.
    body_seat: Dict[Optional[str], Tuple[str, bool]] = {}
    for (body, fam), g in groups.items():
        if not g["seat"] or fam.replace("-elect", "") not in _DISTRICT_FAMILIES:
            continue
        if body not in body_seat or (g["seat_verified"] and not body_seat[body][1]):
            body_seat[body] = (g["seat"], g["seat_verified"])
    for (body, fam), g in groups.items():
        if not g["seat"] and body in body_seat and fam.replace("-elect", "") in _DISTRICT_FAMILIES:
            g["seat"], g["seat_verified"] = body_seat[body]

    out: "OrderedDict[Optional[str], Row]" = OrderedDict()
    for (body, _fam), g in groups.items():
        o = out.setdefault(body, {"organization_id": orgs[body]["organization_id"] if body else None,
                                  "organization": orgs[body]["organization"] if body else None,
                                  "last_seen": None, "roles": []})
        titles = g.pop("titles")
        if _fam == "member":  # say it the way the records mostly do ("Commissioner" on a "Committee")
            common = [t for t, _n in titles.most_common() if t in _MEMBER_LABELS]
            if common:
                g["label"] = common[0]
        g["variants"] = sorted(t for t in titles if t and t.lower() != g["label"].lower())
        o["roles"].append(g)
        if g["last_seen"] and (o["last_seen"] is None or g["last_seen"] > o["last_seen"]):
            o["last_seen"] = g["last_seen"]
    for o in out.values():
        o["roles"].sort(key=lambda g: (g["last_seen"] is None, -(g["last_seen"].toordinal() if g["last_seen"] else 0)))
    result = list(out.values())
    result.sort(key=lambda o: (o["organization"] is None,
                               -(o["last_seen"].toordinal() if o["last_seen"] else 0)))
    return result


def condense_members(rows: List[Row]) -> List[Row]:
    """Reverse view (people in a body): one line per person, their roles joined ('Chair, Commissioner')."""
    people: "OrderedDict[int, Row]" = OrderedDict()
    for r in rows:
        p = people.setdefault(r["id"], {"id": r["id"], "canonical_name": r["canonical_name"], "labels": [],
                                        "last_seen_date": r.get("last_seen_date")})
        label = role_family(r.get("role_title", ""), r.get("organization"))[1]
        if label not in p["labels"]:
            p["labels"].append(label)
        if r.get("last_seen_date") and (p["last_seen_date"] is None or r["last_seen_date"] > p["last_seen_date"]):
            p["last_seen_date"] = r["last_seen_date"]
    for p in people.values():
        p["role_title"] = ", ".join(p["labels"])
    return sorted(people.values(), key=lambda p: (p["last_seen_date"] is None,
                                                   -(p["last_seen_date"].toordinal() if p["last_seen_date"] else 0)))


# ---------------------------------------------------------------------------
# Who is in a body *now*
# ---------------------------------------------------------------------------
# Families that hold a seat on a body (vs. staff: clerk, manager, engineer, analyst...). A liaison sits on
# another body and reports to this one, so they're listed with staff.
SEAT_FAMILIES = {"chair", "vice-chair", "co-chair", "mayor", "mayor-pro-tem", "councilmember", "supervisor",
                 "trustee", "president", "vice-president", "member"}
_NOT_YET = re.compile(r"\b(candidate|nominee|applicant|challenger|running)\b", re.I)
_FORMER = re.compile(r"\b(former|ex|past|retired|outgoing)\b", re.I)
_PLACEHOLDER = re.compile(r"\s\([^)]*\)$")          # "Hallworth (Woodland)": a surname we couldn't place
RECENT_DAYS = 365          # no minutes: seat-holders seen within this long of the body's latest member record


def _person(pid: int, name: str) -> Row:
    return {"id": pid, "canonical_name": name, "seat": [], "staff": [], "incoming": [], "seat_seen": None,
            "seat_first": None, "incoming_seen": None, "last_seen_date": None, "former": False,
            "seat_no": None, "seat_verified": False}


def _day(x):
    return x.date() if hasattr(x, "date") and callable(x.date) else x


def group_members(rows: List[Row], roster: Optional[Dict[int, Row]] = None, roster_date=None) -> Row:
    """Split a body's affiliation rows into current members, former members and staff.

    rows: (id, canonical_name, role_title, last_seen_date, organization) per affiliation row.
    roster: people on the body's most recent roll calls (official minutes) {id: {canonical_name}}, as of
    roster_date. With a roster, current = roster + anyone seen in a seat role after it; without one,
    current = seat-holders seen within RECENT_DAYS of the body's latest seat-holder record.
    Candidates are left out; surname-only placeholders are hidden when a full-name member shares the surname.

    Rows may carry first_seen_date, seat, seat_verified_at (v2.1). Someone only seen as "-Elect" is listed under
    `incoming`, not `current`. Without a roster, a seat has one holder: when two people are recorded in the same
    seat, the one who appeared in it later is current and the other is former."""
    roster = roster or {}
    people: "OrderedDict[int, Row]" = OrderedDict()
    for r in rows:
        role = r.get("role_title") or ""
        if _NOT_YET.search(role):
            continue
        p = people.setdefault(r["id"], _person(r["id"], r["canonical_name"]))
        key, label = role_family(role, r.get("organization"))
        seen = _day(r.get("last_seen_date"))
        if seen and (p["last_seen_date"] is None or seen > p["last_seen_date"]):
            p["last_seen_date"] = seen
        if r.get("seat") and (r.get("seat_verified_at") or not p["seat_verified"]):
            p["seat_no"], p["seat_verified"] = r["seat"], bool(r.get("seat_verified_at"))
        if key.endswith("-elect") and key[:-6] in SEAT_FAMILIES:
            if label not in p["incoming"]:
                p["incoming"].append(label)
            if seen and (p["incoming_seen"] is None or seen > p["incoming_seen"]):
                p["incoming_seen"] = seen
        elif key in SEAT_FAMILIES:
            first = _day(r.get("first_seen_date")) or seen
            if first and (p["seat_first"] is None or first < p["seat_first"]):
                p["seat_first"] = first
            if label not in p["seat"]:
                p["seat"].append(label)
            if seen and (p["seat_seen"] is None or seen > p["seat_seen"]):
                p["seat_seen"] = seen
            if _FORMER.search(role):
                p["former"] = True
        elif label not in p["staff"]:
            p["staff"].append(label)
    for pid, r in roster.items():   # voted in the minutes but never recorded with a role
        p = people.setdefault(pid, _person(pid, r["canonical_name"]))
        if not p["seat"]:
            p["seat"].append(r.get("label") or "Member")
        p["seat_seen"] = max(filter(None, [p["seat_seen"], _day(roster_date)]), default=None)

    seated = [p for p in people.values() if p["seat"]]
    if roster:
        as_of = _day(roster_date)
        current = [p for p in seated if p["id"] in roster
                   or (p["seat_seen"] and as_of and p["seat_seen"] > as_of and not p["former"])]
        latest = max([as_of] + [p["seat_seen"] for p in current if p["seat_seen"]])
    else:
        latest = max((p["seat_seen"] for p in seated if p["seat_seen"]), default=None)
        current = [p for p in seated if latest and p["seat_seen"] and not p["former"]
                   and (latest - p["seat_seen"]).days <= RECENT_DAYS]
        newest: Dict[str, Row] = {}   # seat -> its most recent holder
        for p in current:
            if p["seat_no"] and p["seat_first"]:
                q = newest.get(p["seat_no"])
                if q is None or p["seat_first"] > q["seat_first"]:
                    newest[p["seat_no"]] = p
        current = [p for p in current if not (p["seat_no"] and p["seat_first"]) or newest[p["seat_no"]] is p]
    cur_ids = {p["id"] for p in current}
    incoming = [p for p in people.values() if p["incoming"] and not p["seat"] and p["id"] not in cur_ids
                and not (latest and p["incoming_seen"] and (latest - p["incoming_seen"]).days > RECENT_DAYS)]

    # hide "Castell" / "Hallworth (Woodland)" when "David Castell" is listed
    def surname(p):
        return _PLACEHOLDER.sub("", p["canonical_name"]).split()[-1].lower()
    full = {surname(p) for p in people.values() if len(p["canonical_name"].split()) > 1
            and not _PLACEHOLDER.search(p["canonical_name"])}

    def weak(p):
        return (len(p["canonical_name"].split()) == 1 or _PLACEHOLDER.search(p["canonical_name"])) and surname(p) in full

    def line(p, labels, with_seat=True):
        seat = p["seat_no"] if with_seat else None
        return {"id": p["id"], "canonical_name": p["canonical_name"],
                "role_title": ", ".join(labels + ([seat] if seat else [])),
                "seat": seat, "seat_verified": bool(seat and p["seat_verified"]),
                "last_seen_date": p["seat_seen"] or p["incoming_seen"] or p["last_seen_date"]}

    def recent_first(ps):
        return sorted(ps, key=lambda p: (p["last_seen_date"] is None,
                                         -(p["last_seen_date"].toordinal() if p["last_seen_date"] else 0),
                                         p["canonical_name"]))
    return {
        "as_of": latest,
        "from_minutes": bool(roster),
        "current": sorted((line(p, p["seat"]) for p in current if not weak(p)), key=lambda p: p["canonical_name"]),
        "incoming": sorted((line(p, p["incoming"]) for p in incoming if not weak(p)),
                           key=lambda p: p["canonical_name"]),
        "former": recent_first(line(p, p["seat"]) for p in seated if p["id"] not in cur_ids and not weak(p)),
        "staff": recent_first(line(p, p["staff"], with_seat=False) for p in people.values()
                              if p["staff"] and not p["seat"] and not weak(p)),
    }
