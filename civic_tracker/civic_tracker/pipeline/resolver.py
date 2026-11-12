"""Hybrid pg_trgm + Qwen3 vector entity linker and two-tier cross-article event deduplication."""
from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from psycopg import AsyncConnection

from civic_tracker.db import repositories as repo
from civic_tracker.pipeline import measures, sides
from civic_tracker.pipeline.encoder import Encoder
from civic_tracker.pipeline.jurisdiction import (YOLO_CITIES, alias_ok, compatible_names, expand_acronym,
                                                 is_acronym, is_generic_body, places_in, qualify_generic)
from civic_tracker.pipeline.person_names import (TITLE_ROLE, TITLE_TEXT, clean_person_name, first_name_variants,
                                                 is_placeholder_person, name_parts, persons_compatible,
                                                 surname_form)
from civic_tracker.pipeline.stage2_llm import ArticleExtractionResult, ExtractedEvent, ExtractedInvolvement

log = logging.getLogger(__name__)

EVENT_TIER1_SIM = 0.82
EVENT_TIER2_SIM = 0.74
EVENT_TIER2_MIN_SHARED_ENTITIES = 2

_GOV_HINTS = re.compile(
    r"\b(city|county|state|federal|department|dept|police|sheriff|council|board|commission|agency|court|"
    r"district|office|bureau|administration|ministry|government|legislature|assembly|senate|ice|fbi|dhs)\b",
    re.IGNORECASE,
)


@dataclass
class EntityProfile:
    name: str
    entity_type: str
    role_title: Optional[str] = None
    org_name: Optional[str] = None

    def profile_string(self) -> str:
        return (
            f"Name: {self.name} | Type: {self.entity_type} | "
            f"Role: {self.role_title or 'N/A'} | Org: {self.org_name or 'Local'}"
        )


@dataclass
class ResolutionSummary:
    entity_ids: Dict[str, int]
    event_ids: List[int]
    events_created: int
    events_merged: int


def _key(name: str) -> str:
    return " ".join(name.split()).lower()


_HONORIFIC = re.compile(
    r"^(?:(?:former|acting|interim|incumbent|retired)\s+)?"
    r"(?:rep|representative|sen|senator|assemblymember|assemblyman|assemblywoman|supervisor|mayor|vice\s+mayor|"
    r"councilmember|council\s+member|councilman|councilwoman|gov|governor|lt\.?\s+gov|president|chancellor|"
    r"superintendent|trustee|judge|justice|chief|sheriff|deputy|sgt|sergeant|lt|lieutenant|capt|captain|"
    r"officer|detective|dr|prof|professor|mr|mrs|ms|mx|hon|honorable|rev|reverend)\.?\s+",
    re.IGNORECASE,
)


def strip_honorific(name: str) -> str:
    """'Rep. Mike Tolliver' -> 'Mike Tolliver'. A title + single surname is kept intact
    ('Officer Pemberton') until a first name is known, per the naming rule."""
    m = _HONORIFIC.match(name)
    if not m:
        return name
    rest = name[m.end():].strip()
    return rest if len(rest.split()) >= 2 else name


def _clean(name: str) -> str:
    return strip_honorific(" ".join(name.split()).strip(" .,;:"))


def collapse_surnames(profiles: Dict[str, "EntityProfile"]) -> Dict[str, str]:
    """Within one article: 'DA Ferrell' / 'Ferrell' -> the article's only full-name person with that surname;
    a bare first name ('Pat', no title) -> the article's only full-name person with that first name (and
    nobody with it as a surname)."""
    by_last: Dict[str, List[str]] = {}
    by_first: Dict[str, List[str]] = {}
    for k, p in profiles.items():
        parts = name_parts(p.name) if p.entity_type == "PERSON" else None
        if parts:
            by_last.setdefault(parts[2], []).append(k)
            for f in first_name_variants(parts[0]):
                by_first.setdefault(f, []).append(k)
    renamed: Dict[str, str] = {}
    for k, p in profiles.items():
        sf = surname_form(p.name) if p.entity_type == "PERSON" else None
        if not sf:
            continue
        title, tok = sf
        if len(by_last.get(tok, [])) == 1:
            # "Mayor Hallworth" is not the article's "Sarah Hallworth, City Clerk": the title must fit the role
            # the article gives the full-name person (when it gives one)
            role = (profiles[by_last[tok][0]].role_title or "").lower()
            want = TITLE_ROLE.get(title or "")
            if not (want and role and want not in role):
                renamed[k] = by_last[tok][0]
        elif title is None and not by_last.get(tok) and len(set(by_first.get(tok, []))) == 1:
            renamed[k] = by_first[tok][0]
    return renamed


def _guess_org_type(name: str) -> str:
    return "GOVERNMENT_BODY" if _GOV_HINTS.search(name) else "ORGANIZATION"


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _as_aware(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def collect_profiles(result: ArticleExtractionResult) -> Dict[str, EntityProfile]:
    """One profile per distinct (case-insensitive) entity name, enriched with role/org."""
    profiles: Dict[str, EntityProfile] = {}
    for ev in result.events:
        for inv in ev.involvements:
            name = _clean(inv.entity_name)
            if inv.entity_type == "PERSON":
                name = clean_person_name(name)
                if is_placeholder_person(name):
                    continue  # "the victim", "Jane Doe #2": not a trackable person
            if name:
                profiles.setdefault(_key(name), EntityProfile(name, inv.entity_type))
    for aff in result.affiliations:
        person = clean_person_name(_clean(aff.person_name))
        if not person or is_placeholder_person(person):
            continue
        p = profiles.setdefault(_key(person), EntityProfile(person, "PERSON"))
        p.role_title = p.role_title or aff.role_title
        if aff.organization_name:
            org = _clean(aff.organization_name)
            p.org_name = p.org_name or org
            profiles.setdefault(_key(org), EntityProfile(org, _guess_org_type(org)))
    return profiles


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PersonContext:
    """What an article says about where/when a person appears; resolves weak names ("Ferrell", "Mayor Ruelas")."""
    jurisdiction: Optional[str] = None       # the Yolo city/county the article is about
    when: Optional[date] = None              # article date
    org_ids: FrozenSet[int] = field(default_factory=frozenset)  # organisations/bodies named in the article


CONTEXT_YEARS = 4  # an affiliation counts as evidence within this many years of its first/last sighting


def _is_exact(name: str, row: dict, person: bool = False) -> bool:
    """Exact canonical name or alias. For persons, weak aliases ("Ferrell", "DA Ferrell") are NOT used: they are a
    record of how someone was called, and the same weak name can mean someone else later."""
    k = _key(name)
    aliases = [a for a in (row.get("aliases") or []) if not (person and surname_form(a))]
    return k == _key(row["canonical_name"]) or any(k == _key(a) for a in aliases)


def _strong_names(row: dict) -> List[str]:
    return [n for n in [row["canonical_name"], *(row.get("aliases") or [])] if name_parts(n)]


async def resolve_or_create_entity(
    conn: AsyncConnection, profile: EntityProfile, embedding: Sequence[float],
    ctx: Optional[PersonContext] = None,
) -> Tuple[int, bool]:
    """Return (entity_id, created).

    Exact name/alias matches are trusted. A fuzzy (trigram/vector) match is accepted only when the two names
    could be the same body (jurisdiction.compatible_names: same place, same service, same kind, same district
    number) - so "West Sacramento Police Department" no longer lands on "Sacramento Police Department" - or, for
    persons, the same full name (person_names.persons_compatible: nicknames, spelling slips, no conflicting
    middle initials). A new alias is learned only when it passes jurisdiction.alias_ok.

    Weak person names ("Ferrell", "DA Ferrell", "Pat") never match by name: see _resolve_weak_person."""
    person = profile.entity_type == "PERSON"
    if person and surname_form(profile.name):
        return await _resolve_weak_person(conn, profile, embedding, ctx or PersonContext())
    candidates = await repo.find_matching_entities(conn, profile.name, profile.entity_type, embedding)
    for match in candidates:
        if _is_exact(profile.name, match, person):
            return match["id"], False
        if person:
            if not any(persons_compatible(profile.name, n) for n in _strong_names(match)):
                continue
        elif not compatible_names(profile.name, match["canonical_name"]):
            continue
        if alias_ok(profile.name, match["canonical_name"], profile.entity_type):
            await repo.add_entity_alias(conn, match["id"], profile.name)
        return match["id"], False
    known = await (_same_full_name(conn, profile.name) if person else _other_org_type(conn, profile))
    if known is not None:
        if person:
            await repo.add_entity_alias(conn, known, profile.name)
        return known, False
    return await repo.insert_entity(conn, profile.name, profile.entity_type, embedding), True


async def _same_full_name(conn: AsyncConnection, name: str) -> Optional[int]:
    """'Al Okonkwo' -> the one known 'Albert Okonkwo' (nickname / initial / spelling slip, same surname)."""
    parts = name_parts(name)
    if not parts:
        return None
    cur = await conn.execute(
        """SELECT id, canonical_name, aliases FROM TB_Entity WHERE entity_type = 'PERSON'
             AND (LOWER(canonical_name) LIKE %(p)s OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE LOWER(a) LIKE %(p)s))
           LIMIT 50""", {"p": f"% {parts[2]}%"})
    hits = {r["id"] for r in await cur.fetchall() if any(persons_compatible(name, n) for n in _strong_names(r))}
    return hits.pop() if len(hits) == 1 else None


async def _other_org_type(conn: AsyncConnection, profile: EntityProfile) -> Optional[int]:
    """The same exact name filed under the other organisation type (ORGANIZATION vs GOVERNMENT_BODY)."""
    if profile.entity_type not in ("ORGANIZATION", "GOVERNMENT_BODY"):
        return None
    cur = await conn.execute(
        """SELECT id FROM TB_Entity WHERE entity_type IN ('ORGANIZATION', 'GOVERNMENT_BODY') AND entity_type <> %(t)s
             AND (LOWER(canonical_name) = LOWER(%(n)s) OR LOWER(%(n)s) = ANY(SELECT LOWER(unnest(aliases))))
           LIMIT 2""", {"t": profile.entity_type, "n": profile.name})
    rows = await cur.fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def _allowed_places(jurisdiction: Optional[str]) -> Set[str]:
    if not jurisdiction:
        return set()
    if jurisdiction == "Yolo County":
        return {"Yolo County", *YOLO_CITIES, "UC Davis"}
    if jurisdiction in YOLO_CITIES or jurisdiction == "UC Davis":
        return {jurisdiction, "Yolo County", "Davis" if jurisdiction == "UC Davis" else jurisdiction}
    return {jurisdiction}


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v


def _in_window(first, last, when: Optional[date]) -> bool:
    first, last = _as_date(first), _as_date(last)
    if when is None or (first is None and last is None):
        return True
    span = timedelta(days=365 * CONTEXT_YEARS)
    return (first or last) - span <= when <= (last or first) + span


async def _contextual_person(conn: AsyncConnection, title: Optional[str], tok: str,
                             ctx: PersonContext) -> Optional[int]:
    """Which known full-name person does a weak name mean, given the article's context? Evidence per candidate
    (all within CONTEXT_YEARS of the article date):
      role  - an affiliation whose role fits the title ("Mayor" for "Mayor Hallworth"), or the person's own
              articles call them that ("the Honorable Daniel M. Brandt" for "Judge Brandt");
      org   - an affiliation with an organisation named in this article;
      place - an affiliation with a body of the article's city/county.
    A title with a known role needs role evidence plus org/place (or an article with no place); a bare surname
    or honorific needs org/place evidence. Exactly one candidate must qualify (ties go to org evidence)."""
    cur = await conn.execute(
        """SELECT id, canonical_name, aliases FROM TB_Entity WHERE entity_type = 'PERSON'
             AND (LOWER(canonical_name) LIKE %(p)s OR LOWER(canonical_name) LIKE %(h)s
                  OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE LOWER(a) LIKE %(p)s))
           LIMIT 60""", {"p": f"% {tok}", "h": f"%-{tok}"})
    cands: Dict[int, Tuple[str, str]] = {}   # id -> (first name, canonical)
    for r in await cur.fetchall():
        for n in _strong_names(r):
            parts = name_parts(n)
            if parts and tok in {parts[2], *parts[2].split("-")}:
                cands[r["id"]] = (parts[0], r["canonical_name"])
                break
    if not cands:
        return None
    ids = list(cands)
    cur = await conn.execute(
        """SELECT af.entity_id, LOWER(af.role_title) AS role, af.organization_id, o.canonical_name AS org,
                  af.first_seen_date, af.last_seen_date
           FROM TB_Entity_Affiliation af LEFT JOIN TB_Entity o ON o.id = af.organization_id
           WHERE af.entity_id = ANY(%s)""", (ids,))
    affs = await cur.fetchall()
    want_role = TITLE_ROLE.get(title or "")
    places = _allowed_places(ctx.jurisdiction)
    ev: Dict[int, Dict[str, bool]] = {i: {"role": False, "org": False, "place": False} for i in ids}
    for a in affs:
        if not _in_window(a["first_seen_date"], a["last_seen_date"], ctx.when):
            continue
        e = ev[a["entity_id"]]
        if want_role and want_role in (a["role"] or ""):
            e["role"] = True
        if a["organization_id"] in ctx.org_ids:
            e["org"] = True
        if places & (places_in(a["org"] or "") | places_in(a["role"] or "")):
            e["place"] = True
    if want_role:
        for i in ids:  # "Judge Brandt": the person's own articles say "Judge/the Honorable Daniel M. Brandt"
            if ev[i]["role"]:
                continue
            alts = TITLE_TEXT.get(title, re.escape(title).replace(r"\ ", " ").replace(" ", r"\s+"))
            pattern = (rf"\m(?:{alts})\.?\s+(?:{re.escape(cands[i][0])}\s+(?:[[:alpha:]]\.?\s+)?)?"
                       rf"{re.escape(tok)}\M")
            cur = await conn.execute(
                """SELECT 1 FROM TB_Entity_Event ee JOIN TB_Article a ON a.id = ee.source_article_id
                   WHERE ee.entity_id = %s AND a.full_text ~* %s LIMIT 1""", (i, pattern))
            ev[i]["role"] = await cur.fetchone() is not None

    def qualifies(e: Dict[str, bool]) -> bool:
        if want_role:
            return e["role"] and (e["org"] or e["place"] or not places)
        return e["org"] or e["place"]

    ok = [i for i in ids if qualifies(ev[i])]
    if len(ok) > 1:
        ok = [i for i in ok if ev[i]["org"]] or ok
    if len(ok) == 1:
        log.debug("weak name %r %r -> #%s %s (%s)", title, tok, ok[0], cands[ok[0]][1], ev[ok[0]])
        return ok[0]
    return None


async def _resolve_weak_person(conn: AsyncConnection, profile: EntityProfile, embedding: Sequence[float],
                               ctx: PersonContext) -> Tuple[int, bool]:
    """A weak name goes to the person the context points to. Otherwise it is filed under a holding entity
    scoped to the article's city ("Hallworth (Woodland)") for review: never fused across cities by name alone.
    No alias is learned - the same weak name may mean someone else in another article."""
    title, tok = surname_form(profile.name)
    pid = await _contextual_person(conn, title, tok, ctx)
    if pid is not None:
        return pid, False
    name = f"{profile.name} ({ctx.jurisdiction})" if ctx.jurisdiction else profile.name
    cur = await conn.execute(
        "SELECT id FROM TB_Entity WHERE entity_type = 'PERSON' AND LOWER(canonical_name) = LOWER(%s) LIMIT 1",
        (name,))
    row = await cur.fetchone()
    if row:
        return row["id"], False
    return await repo.insert_entity(conn, name, "PERSON", embedding), True


def qualify_profiles(
    profiles: Dict[str, EntityProfile], jurisdiction: Optional[str]
) -> Tuple[Dict[str, EntityProfile], Dict[str, Optional[str]], List[str]]:
    """Rename bare local-body names to their city ("Planning Commission" -> "Woodland Planning Commission").
    Returns (profiles, renamed: old key -> new key or None if dropped, unresolved names). A bare body name that
    can't be placed is DROPPED rather than filed under a catch-all entity."""
    out: Dict[str, EntityProfile] = {}
    renamed: Dict[str, Optional[str]] = {}
    unresolved: List[str] = []
    for k, p in profiles.items():
        if p.entity_type != "PERSON" and is_generic_body(p.name):
            q = qualify_generic(p.name, jurisdiction)
            if not q:
                renamed[k] = None
                unresolved.append(p.name)
                continue
            nk = _key(q)
            renamed[k] = nk
            out.setdefault(nk, EntityProfile(q, p.entity_type, p.role_title, p.org_name))
        else:
            out.setdefault(k, p)
    for p in out.values():  # persons' org context (used in their profile embedding)
        if p.org_name and _key(p.org_name) in renamed:
            nk = renamed[_key(p.org_name)]
            p.org_name = out[nk].name if nk else None
    return out, renamed, unresolved


async def resolve_entities(
    conn: AsyncConnection, encoder: Encoder, profiles: Dict[str, EntityProfile],
    ctx: Optional[PersonContext] = None,
) -> Dict[str, int]:
    """Organisations/bodies first, then persons - so a weak person name can use the article's organisations
    as context (PersonContext.org_ids)."""
    if not profiles:
        return {}
    keys = sorted(profiles, key=lambda k: profiles[k].entity_type == "PERSON")
    embeds = await encoder.embed([profiles[k].profile_string() for k in keys], task="ENTITY_DEDUP")
    ids: Dict[str, int] = {}
    ctx = ctx or PersonContext()
    for k, emb in zip(keys, embeds):
        p = profiles[k]
        pctx = dataclasses.replace(ctx, org_ids=frozenset(
            v for kk, v in ids.items() if profiles[kk].entity_type != "PERSON")) if p.entity_type == "PERSON" else None
        ids[k], _ = await resolve_or_create_entity(conn, p, emb, pctx)
    return ids


async def expand_acronyms(conn: AsyncConnection, profiles: Dict[str, EntityProfile],
                          text: Optional[str]) -> Dict[str, str]:
    """'CDD' -> 'Community Development Department' (then placed in the article's city like any generic body),
    unless the acronym is already a known entity name/alias ('DJUSD', 'FBI'). Returns old key -> new key."""
    renamed: Dict[str, str] = {}
    for k, p in list(profiles.items()):
        if p.entity_type == "PERSON" or not is_acronym(p.name):
            continue
        cur = await conn.execute(
            """SELECT 1 FROM TB_Entity WHERE entity_type <> 'PERSON' AND (canonical_name = %(n)s
                 OR %(n)s = ANY(aliases)) LIMIT 1""", {"n": p.name})
        if await cur.fetchone():
            continue
        long = expand_acronym(p.name, text or "")
        if not long:
            continue
        nk = _key(long)
        profiles.pop(k)
        profiles.setdefault(nk, EntityProfile(long, p.entity_type, p.role_title, p.org_name))
        renamed[k] = nk
    for p in profiles.values():
        if p.org_name and _key(p.org_name) in renamed:
            p.org_name = profiles[renamed[_key(p.org_name)]].name
    return renamed


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def choose_event_match(candidates: List[dict], participant_ids: set[int]) -> Optional[int]:
    """Two-tier merge decision on candidates sorted by cosine_sim DESC."""
    if not candidates:
        return None
    top = candidates[0]
    sim = float(top["cosine_sim"])
    if sim >= EVENT_TIER1_SIM:
        return top["id"]
    if sim >= EVENT_TIER2_SIM:
        shared = participant_ids & set(top.get("existing_entity_ids") or [])
        if len(shared) >= EVENT_TIER2_MIN_SHARED_ENTITIES:
            return top["id"]
    return None


async def resolve_or_create_event(
    conn: AsyncConnection,
    event: ExtractedEvent,
    embedding: Sequence[float],
    participant_ids: set[int],
    published_at: Optional[datetime],
    genre: Optional[str] = None,
) -> Tuple[int, bool]:
    """Return (event_id, created). Events first seen in an opinion piece are created `provisional`; the first
    non-opinion article that merges into one confirms it."""
    event_date = _parse_iso_date(event.event_date_iso)
    target_date = event_date or _as_aware(published_at).date()
    candidates = await repo.find_event_candidates(conn, embedding, target_date)
    match_id = choose_event_match(candidates, participant_ids)
    tags = sorted({t.strip().lower().replace(" ", "-") for t in event.topic_tags if t.strip()})
    if match_id is not None:
        await repo.merge_event_tags(conn, match_id, tags)
        if genre != "opinion":
            await repo.confirm_event(conn, match_id)
        return match_id, False
    new_id = await repo.insert_event(
        conn,
        canonical_title=event.canonical_title.strip(),
        event_type=event.event_type,
        # Fall back to the publication date so the event is visible to the ±21-day dedup window.
        event_date=target_date,
        summary=event.summary.strip(),
        topic_tags=tags,
        embedding=embedding,
        provisional=genre == "opinion",
    )
    return new_id, True


def _merge_involvements(invs: List[Tuple[int, ExtractedInvolvement]]) -> List[Tuple[int, ExtractedInvolvement]]:
    """Collapse duplicate involvements of the same entity within one event (quotes are unioned)."""
    merged: Dict[int, ExtractedInvolvement] = {}
    for eid, inv in invs:
        if eid not in merged:
            merged[eid] = inv.model_copy(deep=True)
        else:
            existing = merged[eid]
            for q in inv.direct_quotes:
                if q not in existing.direct_quotes:
                    existing.direct_quotes.append(q)
            for s in inv.sides:
                if s not in existing.sides:
                    existing.sides.append(s)
    return list(merged.items())


def involvement_document(inv: ExtractedInvolvement) -> str:
    return f"{inv.role_in_event} ({inv.stance}): {inv.action_summary} Quotes: {' '.join(inv.direct_quotes)}"


# ---------------------------------------------------------------------------
# Article-level entry point
# ---------------------------------------------------------------------------
async def resolve_article(
    conn: AsyncConnection,
    encoder: Encoder,
    *,
    article_id: int,
    published_at: Optional[datetime],
    result: ArticleExtractionResult,
    genre: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    allow_votes: bool = False,
    text: Optional[str] = None,
    minutes_body: Optional[str] = None,
) -> ResolutionSummary:
    """Persist one article's Stage 2 output. Caller owns the transaction. `genre` (from triage) marks events
    born in opinion pieces as provisional; each stance row keeps its evidence type for weighting (v1.5).
    `jurisdiction` (the Yolo city the article is about) places bare body names like "Planning Commission".
    `allow_votes` (official minutes only) records the roll calls in ExtractedEvent.votes (v1.8); `minutes_body`
    (doc_meta.body, e.g. "Climate Action Committee") is the body that voted, whatever body the measure belongs to.
    `text` (the article) lets acronyms the article defines be expanded ("Community Development Department (CDD)")."""
    seen_at = _as_aware(published_at)
    motions = 0
    profiles = collect_profiles(result)
    acronyms = await expand_acronyms(conn, profiles, text)
    profiles, renamed, unresolved = qualify_profiles(profiles, jurisdiction)
    for old, new in acronyms.items():  # 'CDD' -> 'community development department' -> 'woodland community ...'
        renamed[old] = renamed.get(new, new)
    # "DA Ferrell" / "Ferrell" -> the article's "Pat Ferrell" (one person, not three)
    for old, new in collapse_surnames(profiles).items():
        profiles.pop(old, None)
        renamed[old] = new
        for k, nk in list(renamed.items()):  # keep chains (generic-body renames) pointing at the final key
            if nk == old:
                renamed[k] = new
    if unresolved:
        log.info("article %s: could not place %s in a city; left out", article_id, unresolved)
        await repo.note_unresolved_bodies(conn, article_id, unresolved)
    ctx = PersonContext(jurisdiction=jurisdiction, when=seen_at.date())
    entity_ids = await resolve_entities(conn, encoder, profiles, ctx)

    def lookup(name: str) -> Optional[int]:
        for k in (_key(_clean(name)), _key(clean_person_name(_clean(name)))):
            k = renamed.get(k, k)
            if k and k in entity_ids:
                return entity_ids[k]
        return None

    # Affiliations
    for aff in result.affiliations:
        person_id = lookup(aff.person_name)
        if person_id is None or not aff.role_title.strip():
            continue
        org_id = lookup(aff.organization_name) if aff.organization_name else None
        await repo.upsert_affiliation(
            conn,
            entity_id=person_id,
            organization_id=org_id,
            role_title=aff.role_title.strip(),
            seen_at=seen_at,
            source_article_id=article_id,
        )

    # Events
    event_ids: List[int] = []
    created = merged = 0
    if result.events:
        event_embeds = await encoder.embed(
            [f"{ev.canonical_title}: {ev.summary}" for ev in result.events], task="EVENT_DEDUP"
        )
        for ev, emb in zip(result.events, event_embeds):
            invs = [(lookup(inv.entity_name), inv) for inv in ev.involvements]
            invs = [(eid, inv) for eid, inv in invs if eid is not None]
            invs = _merge_involvements(invs)
            participant_ids = {eid for eid, _ in invs}

            event_id, was_created = await resolve_or_create_event(conn, ev, emb, participant_ids, published_at,
                                                                  genre=genre)
            created += was_created
            merged += not was_created
            event_ids.append(event_id)
            await repo.link_article_event(conn, article_id, event_id)

            # measures this event is about (any article; opinion pieces only link to existing measures) and,
            # for official minutes only, the recorded roll calls
            action_date = _parse_iso_date(ev.event_date_iso) or _as_aware(published_at).date()
            measures_ok = (ev.measures or (allow_votes and ev.votes)) and await measures.tables_ready(conn)
            if ev.measures and measures_ok:
                await measures.link_event_measures(conn, encoder, event_id=event_id, event=ev,
                                                   action_date=action_date, jurisdiction=jurisdiction,
                                                   lookup=lookup, create=genre != "opinion")
            if allow_votes and ev.votes and measures_ok:
                motions += await measures.store_votes(conn, encoder, article_id=article_id, event_id=event_id,
                                                      event=ev, action_date=action_date,
                                                      jurisdiction=jurisdiction, lookup=lookup,
                                                      minutes_body=minutes_body)

            if not invs:
                continue
            action_embeds = await encoder.embed([involvement_document(inv) for _, inv in invs], task="SEARCH_DOCUMENT")
            for (eid, inv), aemb in zip(invs, action_embeds):
                await repo.upsert_entity_event(
                    conn,
                    entity_id=eid,
                    event_id=event_id,
                    source_article_id=article_id,
                    role_in_event=inv.role_in_event,
                    stance=inv.stance,
                    action_summary=inv.action_summary,
                    direct_quotes=inv.direct_quotes,
                    action_embedding=aemb,
                    evidence_type=inv.evidence,
                    claimed_by=_clean(inv.claimed_by) if inv.claimed_by else None,
                )
            # who is on which side (contested events) and, for split roll calls, aye vs. no
            await sides.store_extracted_sides(conn, event_id=event_id, article_id=article_id, sides=ev.sides,
                                              memberships={eid: inv.sides for eid, inv in invs if inv.sides})
            if allow_votes and ev.votes:
                await sides.rollcall_sides_for_event(conn, event_id=event_id, article_id=article_id)

    log.info("article %s: %d entities, %d events (%d new, %d merged), %d roll-call motion(s)",
             article_id, len(entity_ids), len(event_ids), created, merged, motions)
    return ResolutionSummary(entity_ids=entity_ids, event_ids=event_ids, events_created=created, events_merged=merged)
