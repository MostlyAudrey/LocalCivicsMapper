"""Event sides (v2.0): label normalization, extraction cleanup, backfill answer parsing, resolver storage."""
from datetime import datetime, timezone

import pytest

from civic_tracker.db import repositories as repo
from civic_tracker.pipeline.assign_sides import parse_answer
from civic_tracker.pipeline.resolver import resolve_article
from civic_tracker.pipeline.sides import keyed, side_key
from civic_tracker.pipeline.stage2_llm import (ArticleExtractionResult, ExtractedEvent, ExtractedInvolvement,
                                              ExtractedSide, sanitize_result)

from .conftest import FakeEncoder


def test_side_key_merges_wording_variants_only():
    assert side_key("Jane Roe campaign") == side_key("Jane Roe") == side_key("Jane Roe's supporters") == "jane roe"
    assert side_key("For Measure J") != side_key("Against Measure J")
    assert keyed(["Jane Roe campaign", "jane roe", "Pat Lee"]) == {"jane roe": "Jane Roe campaign", "pat lee": "Pat Lee"}


def _inv(name, stance, sides=()):
    return ExtractedInvolvement(entity_name=name, entity_type="PERSON", role_in_event="r", stance=stance,
                                action_summary="a", sides=list(sides))


def _event(sides, invs):
    return ExtractedEvent(canonical_title="t", event_type="ELECTION", summary="s", topic_tags=["x"],
                          involvements=invs, sides=[ExtractedSide(label=s, kind="candidate") for s in sides])


def test_sanitize_drops_unknown_and_one_sided():
    res = ArticleExtractionResult(affiliations=[], events=[
        _event(["Jane Roe campaign", "Pat Lee campaign"],
               [_inv("Jane Roe", "SUPPORT", ["Jane Roe"]), _inv("Al Poe", "OPPOSE", ["Made Up Side"])]),
        _event(["Only One"], [_inv("Jane Roe", "SUPPORT", ["Only One"])]),
    ])
    res, _ = sanitize_result(res)
    contested, single = res.events
    assert [i.sides for i in contested.involvements] == [["Jane Roe"], []]   # unlisted label dropped
    assert single.sides == [] and single.involvements[0].sides == []        # one side is not a contest


def test_parse_answer_keeps_listed_sides_and_known_actors():
    content = ('{"contested": true, "sides": [{"label": "A campaign", "kind": "candidate"}, '
               '{"label": "B campaign", "kind": "candidate"}], "members": [{"id": 1, "sides": ["A"]}, '
               '{"id": 2, "sides": ["B campaign", "Z"]}, {"id": 99, "sides": ["A campaign"]}]}')
    ans = parse_answer(content, {1, 2, 3})
    assert ans.contested and [(m.id, m.sides) for m in ans.members] == [(1, ["A"]), (2, ["B campaign"])]
    assert not parse_answer('{"contested": false}', {1}).contested
    assert not parse_answer('{"contested": true, "sides": [{"label": "A"}], "members": [{"id": 1, "sides": ["A"]}]}',
                            {1}).contested  # one side only


@pytest.mark.asyncio(loop_scope="session")
async def test_resolver_stores_extracted_sides(pool):
    res = ArticleExtractionResult(affiliations=[], events=[_event(
        ["Avery Alpha campaign", "Blair Beta campaign"],
        [_inv("Avery Alpha", "SUPPORT", ["Avery Alpha campaign"]), _inv("Blair Beta", "SUPPORT", ["Blair Beta"]),
         _inv("Riley Reporter", "NEUTRAL")])])
    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            src = await repo.add_source(conn, "Sides Resolver", "https://sides-resolver.example")
            aid = await repo.insert_article(
                conn, source_id=src["id"], url="https://sides-resolver.example/a", title="Poll", author=None,
                published_at=datetime(2036, 3, 1, tzinfo=timezone.utc), full_text="x", content_hash="sides-res-1")
            await resolve_article(conn, FakeEncoder(), article_id=aid,
                                  published_at=datetime(2036, 3, 1, tzinfo=timezone.utc), result=res, genre="news")
            rows = await (await conn.execute(
                """SELECT e.canonical_name, s.label, ev.sides_checked_at IS NOT NULL AS checked
                   FROM TB_Entity_Event ee JOIN TB_Entity e ON e.id = ee.entity_id JOIN TB_Event ev ON ev.id = ee.event_id
                   LEFT JOIN TB_Entity_Event_Side l ON l.entity_event_id = ee.id
                   LEFT JOIN TB_Event_Side s ON s.id = l.side_id
                   WHERE ee.source_article_id = %s ORDER BY 1""", (aid,))).fetchall()
    got = {r["canonical_name"]: r["label"] for r in rows}
    assert got == {"Avery Alpha": "Avery Alpha campaign", "Blair Beta": "Blair Beta campaign", "Riley Reporter": None}
    assert all(r["checked"] for r in rows)
