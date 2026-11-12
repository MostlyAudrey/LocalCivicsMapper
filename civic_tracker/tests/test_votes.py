"""Measures + roll-call votes (v1.8): votes only from official minutes; news links to the same measure."""
from datetime import datetime, timezone

import pytest

from civic_tracker.db import repositories as repo
from civic_tracker.pipeline import measures as M
from civic_tracker.pipeline.resolver import resolve_article
from civic_tracker.pipeline.stage2_llm import (ArticleExtractionResult, ExtractedAffiliation, ExtractedEvent,
                                              ExtractedInvolvement, ExtractedMeasureRef, ExtractedVote)

from .conftest import FakeEncoder

ASYNC = pytest.mark.asyncio(loop_scope="session")


def test_number_key_and_member_names():
    k = lambda n, t="OTHER": M.number_key(ExtractedMeasureRef(title="x", measure_type=t, measure_number=n))
    assert k("Ordinance No. 2654") == "ORD-2654"
    assert k("Resolution 26-031") == "RES-26-031"
    assert k("Measure J", "BALLOT_MEASURE") == "MEASURE-J"
    assert k(None) is None
    assert M.clean_member_name("Mayor Pro Tem Ashby") == "Ashby"
    assert M.clean_member_name("Councilmember Gloria Ravel") == "Gloria Ravel"


@pytest.mark.parametrize("a,b,ok", [
    # different people / offices / dates / meetings are different items
    ("Appointment of Maureen Tull as Vice President of the Woodland Library Board",
     "Appointment of Noel Ashford as President of the Woodland Library Board", False),
    ("Minutes for April 21, 2021 meeting", "Minutes for December 13, 2020 meeting", False),
    ("Consent calendar items 3-10", "Consent calendar items 4 through 15", False),
    # the same item, worded more or less specifically
    ("Village Farms development agreement", "Village Farms Davis development agreement", True),
    ("Downtown parking ordinance", "Ordinance on downtown parking", True),
    ("Fiscal Year 2026/27 Annual Budget", "FY 2026/27 Annual Budget", True),
])
def test_measure_titles_compatible(a, b, ok):
    assert M.titles_compatible(a, b) is ok


def _members():
    return [ExtractedAffiliation(person_name=n, organization_name="Davis City Council", role_title="Councilmember")
            for n in ("Gloria Ravel", "Will Ashby", "Dan Corwin")]


def _ord():
    return ExtractedMeasureRef(title="Downtown parking ordinance", measure_type="ORDINANCE",
                               measure_number="Ordinance No. 2701", body_name="Davis City Council")


@ASYNC
async def test_votes_from_minutes_and_news_link(pool):
    enc = FakeEncoder()
    async with pool.connection() as conn:
        src = await repo.add_source(conn, "Votes Test", "https://votes.example")
        minutes_id = await repo.insert_article(
            conn, source_id=src["id"], url="https://votes.example/minutes-1", title="Davis City Council minutes",
            author=None, published_at=datetime(2026, 3, 3, tzinfo=timezone.utc), full_text="x " * 300,
            content_hash="votes-min-1")
        news_id = await repo.insert_article(
            conn, source_id=src["id"], url="https://votes.example/news-1", title="Council passes parking rules",
            author=None, published_at=datetime(2026, 3, 4, tzinfo=timezone.utc), full_text="y " * 300,
            content_hash="votes-news-1")

        vote = ExtractedVote(measure=_ord(), motion="Adopt Ordinance No. 2701 (downtown parking)", outcome="PASSED",
                             tally="2-1", ayes=["Ravel", "Ashby"], noes=["Corwin"], absent=["Chatham"])
        minutes = ArticleExtractionResult(affiliations=_members(), events=[ExtractedEvent(
            canonical_title="Davis council adopts downtown parking ordinance", event_type="LEGISLATIVE_VOTE",
            event_date_iso="2026-03-03", summary="The council adopted the downtown parking ordinance 2-1.",
            topic_tags=["parking", "downtown"], involvements=[ExtractedInvolvement(
                entity_name="Davis City Council", entity_type="GOVERNMENT_BODY", role_in_event="Adopted it",
                stance="SUPPORT", action_summary="Adopted the ordinance.")],
            measures=[_ord()], votes=[vote])])
        async with conn.transaction():
            await resolve_article(conn, enc, article_id=minutes_id, published_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
                                  result=minutes, genre="official_record", jurisdiction="Davis", allow_votes=True)

        # a news article reporting the vote: linked to the SAME measure, but its vote is NOT recorded
        news = ArticleExtractionResult(affiliations=[], events=[ExtractedEvent(
            canonical_title="Council approves new downtown parking rules", event_type="LEGISLATIVE_VOTE",
            event_date_iso="2026-03-03", summary="Merchants opposed the parking ordinance the council passed.",
            topic_tags=["parking"], involvements=[ExtractedInvolvement(
                entity_name="Davis Downtown Business Association", entity_type="ORGANIZATION",
                role_in_event="Opposed it", stance="OPPOSE", action_summary="Said meters will hurt shops.")],
            measures=[_ord()], votes=[vote.model_copy(update={"ayes": ["Corwin"], "noes": []})])])
        async with conn.transaction():
            await resolve_article(conn, enc, article_id=news_id, published_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
                                  result=news, genre="news", jurisdiction="Davis", allow_votes=False)

        ms = await (await conn.execute("SELECT * FROM TB_Measure")).fetchall()
        assert len(ms) == 1 and ms[0]["number_key"] == "ORD-2701" and ms[0]["status"] == "PASSED"
        links = await (await conn.execute("SELECT how FROM TB_Measure_Event ORDER BY how")).fetchall()
        assert [l["how"] for l in links] == ["explicit", "vote"]  # news event + minutes event
        motions = await (await conn.execute("SELECT * FROM TB_Vote_Motion")).fetchall()
        assert len(motions) == 1 and motions[0]["source_article_id"] == minutes_id  # nothing from the news
        rec = await (await conn.execute(
            "SELECT raw_name, member_name, vote, dissent FROM VW_Voting_Record ORDER BY raw_name")).fetchall()
        got = {r["raw_name"]: (r["member_name"], r["vote"], r["dissent"]) for r in rec}
        assert got["Ravel"] == ("Gloria Ravel", "AYE", False)
        assert got["Corwin"] == ("Dan Corwin", "NO", True)             # surname matched via council affiliation
        assert got["Chatham"] == (None, "ABSENT", False)               # unknown name kept raw, never guessed

        # re-extraction replaces the minutes' roll call
        async with conn.transaction():
            await repo.clear_article_extraction(conn, minutes_id)
        assert (await (await conn.execute("SELECT COUNT(*) AS n FROM TB_Vote_Motion")).fetchone())["n"] == 0


def test_plural_member_titles():
    assert M.clean_member_name("Members Hallworth") == "Hallworth"
    assert M.clean_member_name("Councilmembers Ruelas") == "Ruelas"


@ASYNC
async def test_committee_minutes_vote_belongs_to_committee_and_surname_prefers_seat(pool):
    """A committee voting to support a council ordinance is the committee's roll call; a bare surname is the
    member holding a seat, not the clerk who shares it."""
    enc = FakeEncoder()
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            src = await repo.add_source(conn, "Committee Votes", "https://committee.example")
            aid = await repo.insert_article(
                conn, source_id=src["id"], url="https://committee.example/min", title="Tree Committee minutes",
                author=None, published_at=datetime(2026, 4, 1, tzinfo=timezone.utc), full_text="z " * 300,
                content_hash="committee-min-1")
            affs = [ExtractedAffiliation(person_name="Opal Quimby", organization_name="Davis Tree Committee",
                                         role_title="Commissioner"),
                    ExtractedAffiliation(person_name="Rex Quimby", organization_name="Davis Tree Committee",
                                         role_title="Staff Liaison"),
                    ExtractedAffiliation(person_name="Ida Vance", organization_name="Davis Tree Committee",
                                         role_title="Chair")]
            vote = ExtractedVote(
                measure=ExtractedMeasureRef(title="Street tree ordinance", measure_type="ORDINANCE",
                                            body_name="Davis City Council"),
                motion="Recommend the council adopt the street tree ordinance", outcome="PASSED", tally="2-0",
                ayes=["Quimby", "Vance"])
            res = ArticleExtractionResult(affiliations=affs, events=[ExtractedEvent(
                canonical_title="Tree committee backs street tree ordinance", event_type="MEETING",
                event_date_iso="2026-04-01", summary="The committee recommended the ordinance.",
                topic_tags=["trees"], involvements=[ExtractedInvolvement(
                    entity_name="Davis Tree Committee", entity_type="GOVERNMENT_BODY", role_in_event="Recommended",
                    stance="SUPPORT", action_summary="Recommended adoption.")], votes=[vote])])
            await resolve_article(conn, enc, article_id=aid, published_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
                                  result=res, genre="official_record", jurisdiction="Davis", allow_votes=True,
                                  minutes_body="Tree Committee")
            m = await (await conn.execute(
                """SELECT b.canonical_name FROM TB_Vote_Motion m JOIN TB_Entity b ON b.id = m.body_entity_id
                   WHERE m.source_article_id = %s""", (aid,))).fetchone()
            assert m["canonical_name"] == "Davis Tree Committee"
            who = await (await conn.execute(
                """SELECT e.canonical_name FROM TB_Vote v JOIN TB_Vote_Motion m ON m.id = v.motion_id
                   JOIN TB_Entity e ON e.id = v.entity_id WHERE m.source_article_id = %s AND v.raw_name = 'Quimby'""",
                (aid,))).fetchone()
            assert who["canonical_name"] == "Opal Quimby"
