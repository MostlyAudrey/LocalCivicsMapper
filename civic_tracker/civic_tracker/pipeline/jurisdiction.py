"""Which city/county a local government body belongs to.

Problems this solves (2026-11-09):
  * bare names ("Planning Commission", "City Council", "Police Department (PD)") became catch-all entities that
    mixed Davis, Woodland, Winters and West Sacramento;
  * vector matches fused different bodies and the resolver then learned the wrong alias ("Sacramento Police
    Department" <- "West Sacramento Police Department", "Woodland Police" <- "Woodland Fire Department").

Used by the orchestrator (article jurisdiction hint for Stage 2), the resolver (qualify bare names, refuse
incompatible matches/aliases) and the `entity-cleanup` command (repair existing data).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Set

# ---------------------------------------------------------------------------- places
# Order matters: the more specific pattern comes first and its text is blanked before the next ones run
# ("West Sacramento" must not also count as "Sacramento", "UC Davis" not as the city of Davis).
PLACE_PATTERNS: List[tuple] = [
    ("West Sacramento", r"\bWest\s+Sac(?:ramento)?\b|\bWashington Unified\b"),
    ("Sacramento County", r"\bSacramento\s+County\b|\bCounty\s+of\s+Sacramento\b"),
    ("Sacramento", r"\bSacramento\b"),
    ("UC Davis", r"\bU\.?\s?C\.?\s+Davis\b|\bUniversity\s+of\s+California,?\s+Davis\b|\bUCD\b|\bASUCD\b"),
    ("Davis", r"\bDavis\b(?!\s+(?:Vanguard|Enterprise|Dirt|Political\s+Review|Media\s+Access))|\bDJUSD\b"),
    ("Woodland", r"\bWoodland\b(?!\s+Hills)|\bWJUSD\b"),
    ("Winters", r"\bWinters\b(?!\s+Express)"),
    ("Yolo County", r"\bYolo\b|\bCounty\s+of\s+Yolo\b"),
    ("Solano County", r"\bSolano\b"),
    ("Dixon", r"\bDixon\b"),
    ("Vacaville", r"\bVacaville\b"),
    ("Esparto", r"\bEsparto\b"),
    ("Knights Landing", r"\bKnights\s+Landing\b"),
    ("Clarksburg", r"\bClarksburg\b"),
]
_PLACES = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in PLACE_PATTERNS]
# incorporated Yolo cities: the only jurisdictions a bare *city* body (council, planning commission...) can have
YOLO_CITIES = ("Davis", "Woodland", "West Sacramento", "Winters")
STATE_FED = re.compile(r"\b(California|State|Federal|U\.?\s?S\.?|United\s+States|National|American)\b", re.I)


def places_in(text: str) -> Set[str]:
    return set(place_counts(text))


def place_counts(text: str) -> Counter:
    counts: Counter = Counter()
    rest = text or ""
    for name, rx in _PLACES:
        hits = rx.findall(rest)
        if hits:
            counts[name] += len(hits)
            rest = rx.sub(" ", rest)
    return counts


def infer_jurisdiction(title: str, text: str, source_jurisdiction: Optional[str] = None) -> Optional[str]:
    """The Yolo city an article is mainly about, for qualifying bare body names.
    Dominant city in the article (title hits x3) when it clearly leads; else the source's home city."""
    counts = place_counts(text or "")
    for place, n in place_counts(title or "").items():
        counts[place] += 3 * n
    cities = [(c, counts.get(c, 0)) for c in YOLO_CITIES]
    cities.sort(key=lambda x: -x[1])
    (top, n1), (_, n2) = cities[0], cities[1]
    if n1 >= 2 and n1 >= 2 * n2:
        return top
    if source_jurisdiction in YOLO_CITIES:
        return source_jurisdiction
    if n1 >= 1 and n2 == 0:
        return top
    # county-wide story: only county bodies (Board of Supervisors, Sheriff, DA) can be placed
    if source_jurisdiction == "Yolo County" or counts.get("Yolo County"):
        return "Yolo County"
    return None


# ---------------------------------------------------------------------------- generic body names
# A name made only of these words (and containing a body word) names a *kind* of local body, not one body.
GENERIC_WORDS = set("""
the city town county of and & council councils commission commissions committee subcommittee department dept
departments office board boards division authority agency services service planning police pd fire sheriff sheriff's
supervisors school schools district education trustees manager manager's managers attorney attorney's clerk clerk's
public works parks park recreation community development finance budget fiscal transportation tree trees utilities
utility human relations civic arts art senior seniors citizen citizens open space habitat accountability historical
historic resources resource management natural social climate environmental housing library advisory
economic sustainability water street streets safety bicycling bicycle bike traffic ethnic cultural affairs gender
sexuality youth commission's council's chief staff municipal general plan zoning code enforcement animal
panel review facility pollution control treatment plant wastewater cdbg grant grants
""".split())
# a generic name must contain one of these (a typical LOCAL body); "Office of Traffic Safety" or an "Agency" alone
# is not enough
BODY_WORDS = {"council", "commission", "committee", "subcommittee", "department", "dept", "board", "pd",
              "police", "fire", "sheriff", "manager", "attorney", "clerk", "supervisors", "panel", "facility",
              "plant"}
# generic names that have no body word ("Friends of the Library" belongs to some city's library)
GENERIC_PHRASES = re.compile(r"^friends\s+of\s+the\s+(?:public\s+)?library$", re.I)
COUNTY_BODIES = re.compile(r"\b(board of supervisors|supervisors|sheriff|district attorney|grand jury|"
                           r"county counsel|board of education|probation)\b", re.I)
_PAREN = re.compile(r"\s*\([^)]*\)\s*")
_WORD = re.compile(r"[A-Za-z&']+")


def _strip(name: str) -> str:
    n = _PAREN.sub(" ", name or "").replace("’", "'").strip()
    return re.sub(r"^(the)\s+", "", n, flags=re.I).strip()


def is_generic_body(name: str) -> bool:
    """'Planning Commission', 'City Council', 'Police Department (PD)', 'the Board' -> True.
    'Davis Planning Commission', 'California Energy Commission', 'Delta Protection Commission' -> False."""
    n = _strip(name)
    if not n or places_in(n) or STATE_FED.search(n) or re.search(r"\d", n):
        return False  # numbered units ("Dept. 10" = a courtroom) are specific, not generic
    if GENERIC_PHRASES.match(n):
        return True
    words = [w.lower() for w in _WORD.findall(n)]
    return bool(words) and all(w in GENERIC_WORDS for w in words) and any(w in BODY_WORDS for w in words)


# local-government acronyms staff reports use without spelling out (the city comes from the article)
LOCAL_ACRONYMS = {"CDD": "Community Development Department", "PD": "Police Department",
                  "FD": "Fire Department", "PW": "Public Works Department"}
_ACRONYM = re.compile(r"[A-Z][A-Z&]{1,6}")
_SMALL = {"of", "and", "for", "the", "&", "on", "in", "to"}
_TOKEN = re.compile(r"[A-Za-z][\w'’-]*|&")


def _phrase_before(acr: str, words: List[str]) -> Optional[str]:
    caps: List[str] = []
    picked: List[str] = []
    for w in reversed(words):
        if w.lower() in _SMALL:
            picked.append(w)
            continue
        if not w[:1].isupper():
            break
        picked.append(w)
        caps.append(w)
        if len(caps) == len(acr):
            break
    caps.reverse()
    picked.reverse()
    while picked and picked[0].lower() in _SMALL:
        picked.pop(0)
    if len(caps) == len(acr) and "".join(c[0].upper() for c in caps) == acr:
        return " ".join(picked)
    return None


def is_acronym(name: str) -> bool:
    return bool(_ACRONYM.fullmatch((name or "").strip()))


def expand_acronym(name: str, text: str) -> Optional[str]:
    """'CDD' -> 'Community Development Department' when the article defines it ("Community Development
    Department (CDD)" / "CDD (Community Development Department)"), else from LOCAL_ACRONYMS; None otherwise."""
    acr = (name or "").strip()
    if not is_acronym(acr):
        return None
    letters = acr.replace("&", "")
    for m in re.finditer(rf"\(\s*{re.escape(acr)}\s*\)", text or ""):
        words = _TOKEN.findall(text[max(0, m.start() - 200):m.start()])
        phrase = _phrase_before(letters, words)
        if phrase:
            return phrase
    for m in re.finditer(rf"\b{re.escape(acr)}\s*\(([^)]{{4,120}})\)", text or ""):
        words = _TOKEN.findall(m.group(1))
        phrase = _phrase_before(letters, words)
        if phrase and phrase == " ".join(words):
            return phrase
    return LOCAL_ACRONYMS.get(acr)


def is_bare_place(name: str) -> bool:
    """'Davis', 'City of Woodland'? -> only exact place names ('Davis', 'Winters', 'Yolo County')."""
    n = _strip(name)
    if not n:
        return False
    return n.lower() in {p.lower() for p, _ in PLACE_PATTERNS} or \
        any(re.fullmatch(rx, n, re.I) for _, rx in PLACE_PATTERNS)


_SHORT_FORMS = {"pd": "Police Department", "police": "Police Department", "fire": "Fire Department",
                "council": "City Council", "the council": "City Council", "board": None}


def qualify_generic(name: str, jurisdiction: Optional[str]) -> Optional[str]:
    """'Planning Commission' + 'Woodland' -> 'Woodland Planning Commission'; county bodies take the county:
    'Board of Supervisors' + 'Davis' -> 'Yolo County Board of Supervisors'. None when it can't be placed."""
    if not jurisdiction:
        return None
    n = _strip(name)
    short = n.lower()
    if short in _SHORT_FORMS:
        if _SHORT_FORMS[short] is None:
            return None  # "the Board" alone is too vague to place
        n = _SHORT_FORMS[short]
    n = re.sub(r"^city\s+of\s+", "", n, flags=re.I)
    if COUNTY_BODIES.search(n):
        county = "Sacramento County" if jurisdiction in ("Sacramento", "Sacramento County") else "Yolo County"
        return f"{county} {n}"
    place = "Davis" if jurisdiction == "UC Davis" else jurisdiction
    if place not in YOLO_CITIES:
        return None
    return f"{place} {n}"


# ---------------------------------------------------------------------------- compatibility
_SYNONYMS = {"pd": "police", "transit": "transportation", "rec": "recreation", "dept": "department"}


def _stem(word: str) -> str:
    w = re.sub(r"'s$", "", word.lower().replace("’", "'"))
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    return _SYNONYMS.get(w, w)


# Words that say WHAT a body does. When both names have some, they must be the same set ("Parks and Recreation
# Commission" == "Recreation and Park Commission"; Police != Fire; Police != Police Accountability).
SERVICE_WORDS = {_stem(w) for w in """
police fire sheriff council planning supervisors school education court attorney clerk manager parks recreation
transportation tree utilities finance budget fiscal housing library water arts climate accountability
historical natural social human probation jury elections works health airport sanitation flood levee
""".split()}
_MUNICIPALITY = re.compile(r"(?:the\s+)?(?:city|town|county)\s+of\s+(.+)", re.I)


def is_municipality(name: str) -> bool:
    """'City of Woodland', 'County of Yolo', 'Yolo County', 'Davis' - the place's government as a whole
    (not 'City of Davis Fire Department', which is one of its bodies)."""
    n = _strip(name)
    m = _MUNICIPALITY.fullmatch(n)
    if m:
        rest = m.group(1)
        return is_bare_place(rest) or not ({w.lower() for w in _WORD.findall(rest)} & GENERIC_WORDS)
    return is_bare_place(n)
# What KIND of unit: a commission is not the department it advises; a (sub)committee is not its parent body.
STRUCTURE_CLASS = {"commission": "advisory", "board": "advisory",
                   "committee": "sub", "subcommittee": "sub", "task": "sub",
                   "department": "admin", "dept": "admin", "office": "admin", "division": "admin",
                   "district": "admin", "agency": "admin", "authority": "admin", "unit": "admin"}
_NUM = re.compile(r"(\d+)")  # also inside tokens: "CD04", "CA-04", "District3"
_ORD = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6", "seventh": "7",
        "eighth": "8", "ninth": "9", "tenth": "10", "eleventh": "11", "twelfth": "12"}


def _words(name: str) -> Set[str]:
    return {_stem(w) for w in _WORD.findall(_strip(name or ""))}


def _numbers(name: str) -> Set[str]:
    nums = {str(int(n)) for n in _NUM.findall(name or "")}
    nums |= {_ORD[w] for w in _words(name) if w in _ORD}
    return nums


def compatible_names(a: str, b: str) -> bool:
    """Could `a` and `b` name the same organisation/body? False when they name different places (West
    Sacramento vs Sacramento), do different things (Police vs Fire, Police vs Police Accountability), are a
    different kind of unit (Commission vs Department, a subcommittee vs its council), carry different numbers
    (District 1 vs 3, 9th vs 11th Congressional District), or one is a whole city/county and the other one of its
    bodies (City of Woodland vs Woodland City Council). A name without a place or without service words is
    otherwise allowed ("Davis High" ~ "Davis High School"); bare generic names are refused separately."""
    pa, pb = places_in(a), places_in(b)
    if pa and pb and pa != pb:
        return False
    if _numbers(a) != _numbers(b):
        return False
    wa, wb = _words(a), _words(b)
    sa, sb = wa & SERVICE_WORDS, wb & SERVICE_WORDS
    if sa and sb and sa != sb:
        return False
    if is_municipality(a) != is_municipality(b) and (sa or sb):
        return False
    ca = {STRUCTURE_CLASS[w] for w in wa if w in STRUCTURE_CLASS}
    cb = {STRUCTURE_CLASS[w] for w in wb if w in STRUCTURE_CLASS}
    if ("sub" in ca) != ("sub" in cb):
        other_c, other_s = (cb, sb) if "sub" in ca else (ca, sa)
        if other_c - {"sub"} or "council" in other_s:
            return False  # a (sub)committee of a council/commission/department is its own body
    return not ca or not cb or bool(ca & cb)


def alias_ok(alias: str, canonical: str, entity_type: str) -> bool:
    """May `alias` be stored as an alias of `canonical`? Never a bare generic body name. A place name only for
    the place itself ("County of Yolo" for Yolo County, "UC Davis" for UC Davis administration) - never for one
    of its bodies ("Davis" for Davis City Council). Organisations/bodies must also be compatible."""
    if is_generic_body(alias):
        return False
    if is_bare_place(alias) or is_municipality(alias):
        return places_in(alias) == places_in(canonical) and not (_words(canonical) & SERVICE_WORDS)
    if entity_type == "PERSON":
        from civic_tracker.pipeline.person_names import name_parts, persons_compatible, surname_form
        parts = name_parts(canonical)
        if not parts:
            return True  # the canonical name is itself weak ("Trump"); any form is better
        sf = surname_form(alias)
        if sf:  # weak form ("Ferrell", "DA Ferrell"): kept as a record only, the resolver never matches on it
            return bool(set(sf[1].split("-")) & {parts[0], parts[2], *parts[1], *parts[2].split("-")})
        return persons_compatible(alias, canonical)
    return compatible_names(alias, canonical)


def describe(names: Iterable[str]) -> Dict[str, Sequence[str]]:
    """Debug helper: places / numbers / service words per name."""
    return {n: (sorted(places_in(n)), sorted(_numbers(n)), sorted(_words(n) & SERVICE_WORDS)) for n in names}
