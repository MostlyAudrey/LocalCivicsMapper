"""Local bodies: bare names are placed in a city, different bodies are never fused, bad aliases aren't learned."""
import pytest

from civic_tracker.db import repositories as repo
from civic_tracker.pipeline import jurisdiction as J
from civic_tracker.pipeline.resolver import EntityProfile, qualify_profiles, resolve_or_create_entity

from .conftest import FakeEncoder

ASYNC = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("canonical,alias,ok", [
    ("Sacramento Police Department", "West Sacramento Police Department", False),
    ("Woodland Police Department", "Woodland Fire Department", False),
    ("Woodland Police Department", "Woodland Police", True),
    ("Davis City Council", "Davis", False),
    ("Woodland City Council", "City of Woodland", False),
    ("Yolo County Board of Supervisors District 3", "Yolo County Board of Supervisors", False),
    ("DJUSD School Board Area 1", "DJUSD School Board Area 4", False),
    ("Capay Valley Fire Department", "Capay Valley Fire Protection District", True),
    ("Davis Parks and Recreation Commission", "Davis Parks and Recreation Department", False),
    ("Davis City Council", "Davis City Council Subcommittee", False),
    ("California's 11th Congressional District", "California's 9th Congressional District", False),
    ("Davis Police Department", "Davis Police Accountability Commission", False),
    ("Davis Planning Commission", "Planning Commission", False),  # bare generic names are never aliases
])
def test_alias_rules(canonical, alias, ok):
    assert J.alias_ok(alias, canonical, "GOVERNMENT_BODY") is ok


def test_generic_names_and_qualification():
    assert J.is_generic_body("Planning Commission") and J.is_generic_body("Police Department (PD)")
    assert not J.is_generic_body("Davis Planning Commission")
    assert not J.is_generic_body("California Energy Commission")
    assert not J.is_generic_body("Delta Protection Commission")
    assert J.qualify_generic("Planning Commission", "Woodland") == "Woodland Planning Commission"
    assert J.qualify_generic("Police Department (PD)", "West Sacramento") == "West Sacramento Police Department"
    assert J.qualify_generic("Board of Supervisors", "Davis") == "Yolo County Board of Supervisors"
    assert J.qualify_generic("the council", "Winters") == "Winters City Council"
    assert J.qualify_generic("Planning Commission", None) is None


def test_infer_jurisdiction():
    assert J.infer_jurisdiction("Woodland council approves budget",
                                "The Woodland City Council voted 4-1. Woodland residents spoke.", "Davis") == "Woodland"
    assert J.infer_jurisdiction("Council approves budget", "The council voted.", "Winters") == "Winters"
    assert J.infer_jurisdiction("Council approves budget", "The council voted.", None) is None
    # "West Sacramento" is not Sacramento, and the paper's own name is not a place mention
    assert J.places_in("West Sacramento Police") == {"West Sacramento"}
    assert J.places_in("the Davis Enterprise reported") == set()


def test_qualify_profiles_renames_and_drops():
    profiles = {
        "planning commission": EntityProfile("Planning Commission", "GOVERNMENT_BODY"),
        "the board": EntityProfile("the Board", "GOVERNMENT_BODY"),
        "jane doe": EntityProfile("Jane Doe", "PERSON", "Chair", "Planning Commission"),
    }
    out, renamed, unresolved = qualify_profiles(profiles, "Woodland")
    assert renamed == {"planning commission": "woodland planning commission", "the board": None}
    assert unresolved == ["the Board"]
    assert out["woodland planning commission"].name == "Woodland Planning Commission"
    assert out["jane doe"].org_name == "Woodland Planning Commission"


@ASYNC
async def test_resolver_refuses_incompatible_fuzzy_match(pool):
    enc = FakeEncoder()
    async with pool.connection() as conn:
        p_sac = EntityProfile("Sacramento Police Department", "GOVERNMENT_BODY")
        [e1] = await enc.embed([p_sac.profile_string()], task="ENTITY_DEDUP")
        sac_id, created = await resolve_or_create_entity(conn, p_sac, e1)
        assert created
        # a near-identical embedding, but a different city: must become its own entity, no alias learned
        p_ws = EntityProfile("West Sacramento Police Department", "GOVERNMENT_BODY")
        ws_id, created = await resolve_or_create_entity(conn, p_ws, e1)
        assert created and ws_id != sac_id
        # same body, short form: matched, and learned as an alias
        p_short = EntityProfile("Sacramento Police", "GOVERNMENT_BODY")
        sid, created = await resolve_or_create_entity(conn, p_short, e1)
        assert sid == sac_id and not created
        aliases = (await (await conn.execute("SELECT aliases FROM TB_Entity WHERE id=%s", (sac_id,))).fetchone())["aliases"]
        assert "Sacramento Police" in aliases and "West Sacramento Police Department" not in aliases


def test_surname_forms_collapse_within_article():
    from civic_tracker.pipeline.resolver import collapse_surnames, surname_form
    assert surname_form("District Attorney Ferrell") == ("district attorney", "ferrell")
    assert surname_form("Ferrell") == (None, "ferrell")
    assert surname_form("Pat Ferrell") is None
    p = {"pat ferrell": EntityProfile("Pat Ferrell", "PERSON"), "da ferrell": EntityProfile("DA Ferrell", "PERSON"),
         "ferrell": EntityProfile("Ferrell", "PERSON"), "corwin": EntityProfile("Corwin", "PERSON")}
    assert collapse_surnames(p) == {"da ferrell": "pat ferrell", "ferrell": "pat ferrell"}  # Corwin: no full name


@ASYNC
async def test_title_form_matches_known_official(pool):
    enc = FakeEncoder()
    async with pool.connection() as conn:
        p = EntityProfile("Pat Ferrelltest", "PERSON")
        [e] = await enc.embed([p.profile_string()], task="ENTITY_DEDUP")
        jid, _ = await resolve_or_create_entity(conn, p, e)
        org = EntityProfile("Yolo County District Attorney's Office", "GOVERNMENT_BODY")
        [eo] = await enc.embed([org.profile_string()], task="ENTITY_DEDUP")
        oid, _ = await resolve_or_create_entity(conn, org, eo)
        await conn.execute("INSERT INTO TB_Entity_Affiliation (entity_id, organization_id, role_title, "
                           "first_seen_date, last_seen_date) VALUES (%s, %s, 'District Attorney', NOW(), NOW())",
                           (jid, oid))
        # a later article only says "District Attorney Ferrelltest": same person, alias learned
        t = EntityProfile("District Attorney Ferrelltest", "PERSON")
        [et] = await enc.embed([t.profile_string()], task="ENTITY_DEDUP")
        tid, created = await resolve_or_create_entity(conn, t, et)
        assert tid == jid and not created
        # a different title with no matching role stays separate
        o = EntityProfile("Officer Ferrelltest", "PERSON")
        [eo2] = await enc.embed([o.profile_string()], task="ENTITY_DEDUP")
        oid2, created = await resolve_or_create_entity(conn, o, eo2)
        assert created and oid2 != jid
