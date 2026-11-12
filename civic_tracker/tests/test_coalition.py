"""v1.9 coalition index: events where either side only took part (NEUTRAL / PARTICIPANT) don't dilute alignment."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from civic_tracker.db import repositories as repo

ASYNC = pytest.mark.asyncio(loop_scope="session")


@ASYNC
async def test_participant_events_do_not_dilute_alignment(pool):
    async with pool.connection() as conn:
        src = await repo.add_source(conn, "Coalition Paper", "https://coalition.example")
        art = await repo.insert_article(
            conn, source_id=src["id"], url="https://coalition.example/a", title="t", author=None,
            published_at=datetime(2036, 1, 1, tzinfo=timezone.utc), full_text="text", content_hash="coalition-a")

        async def ent(name):
            return (await (await conn.execute(
                "INSERT INTO TB_Entity (canonical_name, entity_type) VALUES (%s, 'PERSON') RETURNING id",
                (name,))).fetchone())["id"]

        async def event(title):
            return (await (await conn.execute(
                "INSERT INTO TB_Event (canonical_title, event_type, summary) VALUES (%s, 'ELECTION', 's') RETURNING id",
                (title,))).fetchone())["id"]

        cand, mgr = await ent("Quentin Candidate"), await ent("Ursula Manager")
        poll, primary = await event("Coalition poll release"), await event("Coalition primary election")
        for eid, ev, stance in ((cand, poll, "SUPPORT"), (mgr, poll, "SUPPORT"),
                                (cand, primary, "PARTICIPANT"), (mgr, primary, "DEFENSIVE")):
            await conn.execute(
                "INSERT INTO TB_Entity_Event (entity_id, event_id, source_article_id, role_in_event, stance, "
                "action_summary) VALUES (%s, %s, %s, 'r', %s, 'a')", (eid, ev, art, stance))
        row = await (await conn.execute(
            "SELECT shared_events_count, aligned_stance_count, alignment_index FROM VW_Entity_Coalition_Matrix "
            "WHERE entity_a_id = %s AND entity_b_id = %s", (cand, mgr))).fetchone()
        # leave the shared test schema as we found it (test_pipeline_e2e counts every event / article)
        await conn.execute("DELETE FROM TB_Event WHERE id = ANY(%s)", ([poll, primary],))
        await conn.execute("DELETE FROM TB_Entity WHERE id = ANY(%s)", ([cand, mgr],))
        await conn.execute("DELETE FROM TB_Article WHERE id = %s", (art,))
        await conn.execute("DELETE FROM TB_Sources WHERE id = %s", (src["id"],))
    assert row["shared_events_count"] == 2 and row["aligned_stance_count"] == 1
    assert float(row["alignment_index"]) == 1.0


@ASYNC
async def test_sides_decide_alignment_in_contested_events(pool):
    """Poll story: both candidates SUPPORT (their own campaign) -> opposite sides -> opponents; a PAC that OPPOSES
    candidate B sits on A's side -> allies with A; A's manager on A's side -> allies."""
    from civic_tracker.pipeline import sides as S

    async with pool.connection() as conn:
        async with conn.transaction(force_rollback=True):
            src = await repo.add_source(conn, "Sides Paper", "https://sides.example")
            art = await repo.insert_article(
                conn, source_id=src["id"], url="https://sides.example/a", title="t", author=None,
                published_at=datetime(2036, 2, 1, tzinfo=timezone.utc), full_text="text", content_hash="sides-a")

            async def ent(name, etype="PERSON"):
                return (await (await conn.execute(
                    "INSERT INTO TB_Entity (canonical_name, entity_type) VALUES (%s, %s) RETURNING id",
                    (name, etype))).fetchone())["id"]

            a, b, mgr_b, pac = (await ent("Avery Alpha"), await ent("Blair Beta"), await ent("Casey Gamma"),
                                await ent("Delta Action PAC", "ORGANIZATION"))
            ev = (await (await conn.execute(
                "INSERT INTO TB_Event (canonical_title, event_type, summary) VALUES ('Sides poll', 'ELECTION', 's') "
                "RETURNING id")).fetchone())["id"]
            for eid, stance in ((a, "SUPPORT"), (b, "SUPPORT"), (mgr_b, "SUPPORT"), (pac, "OPPOSE")):
                await conn.execute(
                    "INSERT INTO TB_Entity_Event (entity_id, event_id, source_article_id, role_in_event, stance, "
                    "action_summary) VALUES (%s, %s, %s, 'r', %s, 'a')", (eid, ev, art, stance))

            async def pair(x, y):
                return await (await conn.execute(
                    "SELECT alignment_index, same_side_count, opposite_side_count FROM VW_Entity_Coalition_Matrix "
                    "WHERE entity_a_id = %s AND entity_b_id = %s", (x, y))).fetchone()

            assert float((await pair(a, mgr_b))["alignment_index"]) == 1.0    # stance-only: wrongly allies
            side_a = await S.ensure_side(conn, ev, "Avery Alpha campaign", "candidate", "backfill")
            side_b = await S.ensure_side(conn, ev, "Blair Beta campaign", "candidate", "backfill")
            for eid, sid in ((a, side_a), (pac, side_a), (b, side_b), (mgr_b, side_b)):
                await S.set_entity_sides(conn, event_id=ev, entity_id=eid, side_ids=[sid])
            p = await pair(a, mgr_b)
            assert float(p["alignment_index"]) == -1.0 and p["opposite_side_count"] == 1
            assert float((await pair(a, pac))["alignment_index"]) == 1.0      # OPPOSE B = on A's side
            assert float((await pair(a, b))["alignment_index"]) == -1.0
            # an endorser of both (two seats) is allied with both
            both = await ent("Ellis Both")
            await conn.execute(
                "INSERT INTO TB_Entity_Event (entity_id, event_id, source_article_id, role_in_event, stance, "
                "action_summary) VALUES (%s, %s, %s, 'r', 'SUPPORT', 'a')", (both, ev, art))
            await S.set_entity_sides(conn, event_id=ev, entity_id=both, side_ids=[side_a, side_b])
            assert float((await pair(both, a))["alignment_index"]) == 1.0
            assert float((await pair(both, b))["alignment_index"]) == 1.0
