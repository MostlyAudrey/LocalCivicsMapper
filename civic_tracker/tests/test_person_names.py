"""Weak person names resolve from context (role, organisations, city, date), never by name alone."""
from datetime import date

import pytest

from civic_tracker.pipeline import person_names as P
from civic_tracker.pipeline.jurisdiction import alias_ok, expand_acronym, is_generic_body, qualify_generic
from civic_tracker.pipeline.resolver import (EntityProfile, PersonContext, collapse_surnames,
                                             resolve_or_create_entity)

from .conftest import FakeEncoder

ASYNC = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("name,form", [
    ("Ferrell", (None, "ferrell")),
    ("DA Ferrell", ("da", "ferrell")),
    ("Honorable Judge Brandt", ("judge", "brandt")),
    ("Mayor Pro Tempore Ruelas", ("mayor pro tempore", "ruelas")),
    ("City Manager Calder", ("city manager", "calder")),
    ("Pat", (None, "pat")),
    ("Pat Ferrell", None),
    ("Dan Smith", None),  # "Da" is not a title inside "Dan"
])
def test_surname_form(name, form):
    assert P.surname_form(name) == form


@pytest.mark.parametrize("a,b,ok", [
    ("Al Okonkwo", "Albert Okonkwo", True),
    ("Geoff Attardo", "Geoffrey Attardo", True),
    ("Varadan, Jay", "Jay Varadan", True),
    ("Erika Bumgarder", "Erika Bumgardner", True),
    ("Daniel Brandt", "Daniel M. Brandt", True),
    ("Ray Riehle", "Raymond Riehle", True),
    ("Martin Delosangeles", "Martin de los Angeles", True),
    ("Alfonso Sala", "Alfonso Salas", True),
    ("David M. Reed", "David W. Reed", False),
    ("Tom Reed", "Tom Reid", False),
    ("Pat Smith", "Pat Ferrell", False),
    ("Sarah Hallworth", "Richard Hallworth", False),
    ("Ferrell", "Pat Ferrell", False),  # weak names are never "compatible": they need context
])
def test_persons_compatible(a, b, ok):
    assert P.persons_compatible(a, b) is ok


def test_name_cleanup_and_placeholders():
    assert P.clean_person_name("Carol Pellegrin F") == "Carol Pellegrin"
    assert P.clean_person_name("Daniel M. Brandt") == "Daniel M. Brandt"
    assert P.is_placeholder_person("the victim") and P.is_placeholder_person("Jane Doe #2")
    assert not P.is_placeholder_person("Victoria Smith")


def test_person_alias_rules():
    assert alias_ok("Al Okonkwo", "Albert Okonkwo", "PERSON")
    assert alias_ok("Ferrell", "Pat Ferrell", "PERSON")          # kept as a record, never matched on
    assert not alias_ok("David M. Reed", "David W. Reed", "PERSON")
    assert not alias_ok("Brandt", "Pat Ferrell", "PERSON")
    assert alias_ok("Donald Trump", "Trump", "PERSON")          # a weak canonical: the full form is welcome


def test_acronyms_and_new_generic_bodies():
    assert expand_acronym("CDD", "Reviewed by: Brent Mayhew, CDD Director") == "Community Development Department"
    assert expand_acronym("WPCF", "the Water Pollution Control Facility (WPCF) treats") == \
        "Water Pollution Control Facility"
    assert expand_acronym("SCWA", "SCWA (Solano County Water Agency) said") == "Solano County Water Agency"
    assert expand_acronym("XYZQ", "nothing here") is None
    assert is_generic_body("Friends of the Library") and is_generic_body("Review Panel")
    assert qualify_generic("Water Pollution Control Facility", "Woodland") == "Woodland Water Pollution Control Facility"


def test_collapse_respects_the_articles_roles():
    p = {"sarah hallworth": EntityProfile("Sarah Hallworth", "PERSON", "City Clerk"),
         "mayor hallworth": EntityProfile("Mayor Hallworth", "PERSON"),
         "pat ferrell": EntityProfile("Pat Ferrell", "PERSON"),
         "pat": EntityProfile("Pat", "PERSON")}
    # the mayor is not the clerk who wrote the report; "Pat" is the article's only Pat
    assert collapse_surnames(p) == {"jeff": "pat ferrell"}


async def _entity(conn, enc, name, etype="PERSON"):
    p = EntityProfile(name, etype)
    [e] = await enc.embed([p.profile_string()], task="ENTITY_DEDUP")
    eid, _ = await resolve_or_create_entity(conn, p, e)
    return eid


async def _affiliate(conn, pid, oid, role, first, last):
    await conn.execute("INSERT INTO TB_Entity_Affiliation (entity_id, organization_id, role_title, first_seen_date, "
                       "last_seen_date) VALUES (%s, %s, %s, %s, %s)", (pid, oid, role, first, last))


async def _weak(conn, enc, name, ctx):
    p = EntityProfile(name, "PERSON")
    [e] = await enc.embed([p.profile_string()], task="ENTITY_DEDUP")
    return await resolve_or_create_entity(conn, p, e, ctx)


@ASYNC
async def test_weak_names_resolve_from_context(pool):
    enc = FakeEncoder()
    async with pool.connection() as conn:
        council = await _entity(conn, enc, "Woodland City Council", "GOVERNMENT_BODY")
        richard = await _entity(conn, enc, "Richard Hallwtest")
        sarah = await _entity(conn, enc, "Sarah Hallwtest")
        await _affiliate(conn, richard, council, "Mayor", date(2024, 1, 1), date(2026, 6, 1))
        await _affiliate(conn, sarah, council, "City Clerk", date(2024, 1, 1), date(2026, 6, 1))
        woodland = PersonContext("Woodland", date(2026, 3, 1), frozenset({council}))

        assert await _weak(conn, enc, "Mayor Hallwtest", woodland) == (richard, False)
        assert await _weak(conn, enc, "City Clerk Hallwtest", woodland) == (sarah, False)
        # a bare surname with two candidates: held for review, scoped to the city - not fused by name
        hid, created = await _weak(conn, enc, "Hallwtest", woodland)
        assert created and hid not in (richard, sarah)
        name = (await (await conn.execute("SELECT canonical_name FROM TB_Entity WHERE id=%s", (hid,))).fetchone())
        assert name["canonical_name"] == "Hallwtest (Woodland)"
        # no alias was learned on either person
        rows = await (await conn.execute("SELECT aliases FROM TB_Entity WHERE id = ANY(%s)", ([richard, sarah],))).fetchall()
        assert all(not r["aliases"] for r in rows)


@ASYNC
async def test_a_later_namesake_is_not_the_old_official(pool):
    enc = FakeEncoder()
    async with pool.connection() as conn:
        office = await _entity(conn, enc, "Yolo County District Attorney's Office Test", "GOVERNMENT_BODY")
        old = await _entity(conn, enc, "Pat Oldtestda")
        await _affiliate(conn, old, office, "District Attorney", date(2005, 1, 1), date(2010, 1, 1))
        await conn.execute("UPDATE TB_Entity SET aliases = ARRAY['Oldtestda'] WHERE id = %s", (old,))
        # 2009: the DA then
        assert (await _weak(conn, enc, "DA Oldtestda", PersonContext("Yolo County", date(2009, 6, 1))))[0] == old
        # 2030: same surname, but the old DA's role ended two decades ago -> not him, even via the stored alias
        later, _ = await _weak(conn, enc, "Oldtestda", PersonContext("Yolo County", date(2030, 6, 1)))
        assert later != old


@ASYNC
async def test_nicknames_and_org_types(pool):
    enc = FakeEncoder()
    async with pool.connection() as conn:
        albert = await _entity(conn, enc, "Albert Okontest")
        assert await _entity(conn, enc, "Al Okontest") == albert
        reed = await _entity(conn, enc, "David W. Reedtest")
        assert await _entity(conn, enc, "David M. Reedtest") != reed
        body = await _entity(conn, enc, "Zyxwv Community Development Department", "GOVERNMENT_BODY")
        assert await _entity(conn, enc, "Zyxwv Community Development Department", "ORGANIZATION") == body
