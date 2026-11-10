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
