"""Seats (v2.1): a district is a field of the affiliation, never its own entity."""
from datetime import datetime, timezone

import pytest

from civic_tracker.db import repositories as repo
from civic_tracker.pipeline import seats
from civic_tracker.pipeline.resolver import resolve_article
from civic_tracker.pipeline.stage2_llm import ArticleExtractionResult, ExtractedAffiliation

from .conftest import FakeEncoder


def test_normalize_seat():
    assert seats.normalize_seat("district three") == "District 3"
    assert seats.normalize_seat("3rd District") == "District 3"
    assert seats.normalize_seat("Supervisorial District 3") == "District 3"
    assert seats.normalize_seat("Trustee Area 4") == seats.normalize_seat("area 4") == "Area 4"
    assert seats.normalize_seat("Seat a") == "Seat A"
    assert seats.normalize_seat("school district") is None and seats.normalize_seat("At-large") is None


def test_seat_from_role():
    assert seats.seat_from_role("Supervisor, District 3") == ("Supervisor", "District 3")
    assert seats.seat_from_role("District 3 Supervisor") == ("Supervisor", "District 3")
    assert seats.seat_from_role("Trustee (Area 4)") == ("Trustee", "Area 4")
    assert seats.seat_from_role("Mayor") == ("Mayor", None)
    assert seats.seat_from_role("District 3") == ("District 3", None)   # nothing left: not a role + seat


def test_split_seat_only_at_the_end_of_a_name():
    assert seats.split_seat("Riverton City Council District 2") == ("Riverton City Council", "District 2")
    assert seats.split_seat("Marlow County Board of Supervisors, District 3") == (
        "Marlow County Board of Supervisors", "District 3")
    assert seats.split_seat("Marlow County District 2 Office") == ("Marlow County", "District 2")
    assert seats.split_seat("District 3 Office") is None                              # names no body
    assert seats.split_seat("Marlow County District 2 Community Benefit Fund") is None  # a fund, not a seat
    assert seats.split_seat("Riverton Joint Unified School District") is None


def test_seat_like_entities_spare_real_agencies():
    for name in ("Riverton City Council District 2", "Marlow County District 2", "District 3 Office",
                 "County Supervisor", "Riverton School Board Trustee Area 4"):
        assert seats.looks_like_seat_entity(name), name
    for name in ("Reclamation District 900", "Levee District 7", "Riverton Joint Unified School District",
                 "Marlow County Board of Supervisors", "Marlow County District 2 Community Benefit Fund"):
        assert not seats.looks_like_seat_entity(name), name


def _aff(person, role, org, seat=None):
    return ExtractedAffiliation(person_name=person, role_title=role, organization_name=org, seat=seat)


@pytest.mark.asyncio(loop_scope="session")
async def test_resolver_files_seats_under_the_body(pool):
    when = datetime(2037, 2, 1, tzinfo=timezone.utc)
    board = "Marlow County Board of Supervisors"
    first = ArticleExtractionResult(events=[], affiliations=[_aff("Nadia Quist", "Supervisor", board)])
    second = ArticleExtractionResult(events=[], affiliations=[
        _aff("Nadia Quist", "Supervisor", f"{board} District 3"),        # seat written into the body name
        _aff("Tobias Wren", "Supervisor, District 2", board),              # seat written into the role
        _aff("Odile Farrow", "Supervisor", "Marlow County District 5"),   # "<county> District N" + a supervisor
        _aff("Ines Calloway", "Director", "Reclamation District 900"),    # a real agency: untouched
    ])
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            src = await repo.add_source(conn, "Seats Resolver", "https://seats-resolver.example")
            ids = []
            for n, res in enumerate((first, second)):
                aid = await repo.insert_article(
                    conn, source_id=src["id"], url=f"https://seats-resolver.example/{n}", title="Board", author=None,
                    published_at=when, full_text="x", content_hash=f"seats-res-{n}")
                await resolve_article(conn, FakeEncoder(), article_id=aid, published_at=when, result=res, genre="news")
                ids.append(aid)
            rows = await (await conn.execute(
                """SELECT p.canonical_name AS person, o.canonical_name AS org, a.role_title, a.seat
                   FROM TB_Entity_Affiliation a JOIN TB_Entity p ON p.id = a.entity_id
                   JOIN TB_Entity o ON o.id = a.organization_id
                   WHERE a.source_article_id = ANY(%s) ORDER BY 1""", (ids,))).fetchall()
            stray = await (await conn.execute(
                "SELECT canonical_name FROM TB_Entity WHERE canonical_name ILIKE 'Marlow County%%District%%'")).fetchall()
    got = {r["person"]: (r["org"], r["role_title"], r["seat"]) for r in rows}
    assert got == {
        "Nadia Quist": (board, "Supervisor", "District 3"),
        "Tobias Wren": (board, "Supervisor", "District 2"),
        "Odile Farrow": (board, "Supervisor", "District 5"),
        "Ines Calloway": ("Reclamation District 900", "Director", None),
    }
    assert stray == []   # no "... District N" entity was created


@pytest.mark.asyncio(loop_scope="session")
async def test_article_never_replaces_a_verified_seat(pool):
    when = datetime(2037, 3, 1, tzinfo=timezone.utc)
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            person = await repo.insert_entity(conn, "Nadia Quist", "PERSON", FakeEncoder()._vec("p"))
            body = await repo.insert_entity(conn, "Marlow County Board of Supervisors", "GOVERNMENT_BODY",
                                            FakeEncoder()._vec("b"))
            kw = dict(entity_id=person, organization_id=body, role_title="Supervisor", seen_at=when,
                      source_article_id=None, with_seat=True)
            await repo.upsert_affiliation(conn, seat="District 3", **kw)
            await repo.upsert_affiliation(conn, seat=None, **kw)           # a later article without the seat
            seat = lambda: conn.execute("SELECT seat FROM TB_Entity_Affiliation WHERE entity_id = %s", (person,))
            assert (await (await seat()).fetchone())["seat"] == "District 3"
            await conn.execute("UPDATE TB_Entity_Affiliation SET seat_verified_at = NOW() WHERE entity_id = %s",
                               (person,))
            await repo.upsert_affiliation(conn, seat="District 4", **kw)   # an article that gets it wrong
            assert (await (await seat()).fetchone())["seat"] == "District 3"
