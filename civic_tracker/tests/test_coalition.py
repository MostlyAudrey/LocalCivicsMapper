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
