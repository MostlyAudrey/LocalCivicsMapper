"""Raw-SQL data access. Every function takes an open AsyncConnection (dict_row) so callers
control transaction boundaries. All SQL is parameterised."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from civic_tracker.db.connection import to_vector_literal

Row = Dict[str, Any]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
async def add_source(
    conn: AsyncConnection,
    name: str,
    base_url: str,
    sitemap_url: Optional[str] = None,
    rss_url: Optional[str] = None,
    scrape_config: Optional[Dict[str, Any]] = None,
    is_active: bool = True,
) -> Row:
    from psycopg.types.json import Jsonb

    cur = await conn.execute(
        """
        INSERT INTO TB_Sources (name, base_url, sitemap_url, rss_url, scrape_config, is_active)
        VALUES (%s, %s, %s, %s, COALESCE(%s, '{}'::jsonb), %s)
        ON CONFLICT (name) DO UPDATE
            SET base_url = EXCLUDED.base_url,
                sitemap_url = COALESCE(EXCLUDED.sitemap_url, TB_Sources.sitemap_url),
                rss_url = COALESCE(EXCLUDED.rss_url, TB_Sources.rss_url),
                scrape_config = CASE WHEN %s::jsonb IS NULL THEN TB_Sources.scrape_config
                                     ELSE EXCLUDED.scrape_config END,
                is_active = EXCLUDED.is_active
        RETURNING *
        """,
        (name, base_url.rstrip("/"), sitemap_url, rss_url,
         Jsonb(scrape_config) if scrape_config is not None else None, is_active,
         Jsonb(scrape_config) if scrape_config is not None else None),
    )
    return await cur.fetchone()  # type: ignore[return-value]


async def list_sources(conn: AsyncConnection, source_id: Optional[int] = None, active_only: bool = True) -> List[Row]:
    cur = await conn.execute(
        """
        SELECT * FROM TB_Sources
        WHERE (%(sid)s::INT IS NULL OR id = %(sid)s::INT)
          AND (NOT %(active)s OR is_active)
        ORDER BY id
        """,
        {"sid": source_id, "active": active_only},
    )
    return await cur.fetchall()


async def touch_source_crawled(conn: AsyncConnection, source_id: int) -> None:
    await conn.execute("UPDATE TB_Sources SET last_crawled_at = NOW() WHERE id = %s", (source_id,))


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------
async def filter_new_urls(conn: AsyncConnection, urls: Sequence[str]) -> List[str]:
    """Return the subset of `urls` not already in TB_Article, preserving order."""
    if not urls:
        return []
    cur = await conn.execute("SELECT url FROM TB_Article WHERE url = ANY(%s)", (list(urls),))
    existing = {r["url"] for r in await cur.fetchall()}
    seen: set[str] = set()
    out: List[str] = []
    for u in urls:
        if u not in existing and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def insert_article(
    conn: AsyncConnection,
    *,
    source_id: int,
    url: str,
    title: str,
    author: Optional[str],
    published_at: Optional[datetime],
    full_text: str,
    content_hash: str,
    genre: Optional[str] = None,
    doc_meta: Optional[dict] = None,
) -> Optional[int]:
    """Insert a PENDING article. Returns the new id, or None if the URL already exists.

    genre given (e.g. 'official_record'): the crawler already knows what this is, so the row is stored as
    triaged (local, triaged_at set) and the LLM triage is skipped. doc_meta (v1.6 column) is only written
    when given, so news crawling keeps working before the v1.6 migration is applied."""
    if genre is None and doc_meta is None:
        cur = await conn.execute(
            """
            INSERT INTO TB_Article (source_id, url, title, author, published_at, full_text, content_hash, processing_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING')
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            (source_id, url, title, author, published_at, full_text, content_hash),
        )
    else:
        cur = await conn.execute(
            """
            INSERT INTO TB_Article (source_id, url, title, author, published_at, full_text, content_hash,
                                    processing_status, genre, yolo_relevance, triaged_at, doc_meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, 'local', NOW(), %s)
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            (source_id, url, title, author, published_at, full_text, content_hash, genre,
             Jsonb(doc_meta) if doc_meta is not None else None),
        )
    row = await cur.fetchone()
    return row["id"] if row else None


async def hash_exists(conn: AsyncConnection, content_hash: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM TB_Article WHERE content_hash = %s LIMIT 1", (content_hash,))
    return (await cur.fetchone()) is not None


async def claim_next_article(conn: AsyncConnection, oldest: bool = False) -> Optional[Row]:
    """Atomically claim the most recently published unparsed article (newest news first), or with
    oldest=True the OLDEST one (API burst mode works the backlog from the other end).

    PENDING / STAGE1_DONE -> IN_PROGRESS. Returns the row plus `prev_status` so the caller knows
    whether Stage 1 already ran. Safe with several concurrent claimers (SKIP LOCKED)."""
    cur = await conn.execute(
        """
        WITH next AS (
            SELECT id, processing_status AS prev_status
            FROM TB_Article
            WHERE processing_status IN ('PENDING', 'STAGE1_DONE')
            ORDER BY CASE WHEN %(oldest)s THEN published_at END ASC NULLS LAST,
                     CASE WHEN NOT %(oldest)s THEN published_at END DESC NULLS LAST, id DESC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE TB_Article a
        SET processing_status = 'IN_PROGRESS', claimed_at = NOW()
        FROM next
        WHERE a.id = next.id
        RETURNING a.id, a.source_id, a.url, a.title, a.published_at, a.full_text, a.resolved_text,
                  a.evidence_bundle, a.candidate_entities, next.prev_status,
                  a.triaged_at, a.skip_reason, a.genre, a.doc_meta
        """,
        {"oldest": oldest},
    )
    return await cur.fetchone()


async def save_triage(conn: AsyncConnection, article_id: int, *, keep: bool, genre: Optional[str],
                      relevance: Optional[str], reason: Optional[str]) -> None:
    """Record the triage decision. A skipped article is finished (SKIPPED); a kept one stays claimed."""
    await conn.execute(
        """
        UPDATE TB_Article
        SET genre = %(genre)s, yolo_relevance = %(rel)s, triaged_at = NOW(),
            skip_reason = CASE WHEN %(keep)s THEN NULL ELSE %(reason)s END,
            processing_status = CASE WHEN %(keep)s THEN processing_status ELSE 'SKIPPED' END,
            processed_at = CASE WHEN %(keep)s THEN processed_at ELSE NOW() END,
            claimed_at = CASE WHEN %(keep)s THEN claimed_at ELSE NULL END
        WHERE id = %(id)s
        """,
        {"id": article_id, "keep": keep, "genre": genre, "rel": relevance, "reason": (reason or "")[:500]},
    )


async def claim_for_reextract(conn: AsyncConnection, article_id: int) -> Optional[Row]:
    """Claim an already PROCESSED article for re-extraction (PROCESSED -> IN_PROGRESS). It never re-enters
    the queue, so the newest-first slots can't pick it up. prev_status STAGE1_DONE => Stage 1 is reused."""
    cur = await conn.execute(
        """
        UPDATE TB_Article SET processing_status = 'IN_PROGRESS', claimed_at = NOW()
        WHERE id = %s AND processing_status = 'PROCESSED' AND evidence_bundle IS NOT NULL
        RETURNING id, source_id, url, title, published_at, full_text, resolved_text, evidence_bundle,
                  candidate_entities, 'STAGE1_DONE' AS prev_status, triaged_at, skip_reason, genre, doc_meta
        """,
        (article_id,),
    )
    return await cur.fetchone()


async def clear_article_extraction(conn: AsyncConnection, article_id: int) -> None:
    """Remove an article's previous Stage 2 output before re-resolving it (inside the resolve transaction).
    Disputed rows are kept: an admin's dispute survives re-extraction."""
    await conn.execute("DELETE FROM TB_Entity_Event WHERE source_article_id = %s AND NOT disputed", (article_id,))
    await conn.execute("DELETE FROM TB_Article_Event WHERE article_id = %s", (article_id,))
    cur = await conn.execute("SELECT to_regclass('tb_vote_motion') IS NOT NULL AS ok")
    if (await cur.fetchone())["ok"]:  # v1.8: the minutes' roll calls are re-recorded from the new extraction
        await conn.execute("DELETE FROM TB_Vote_Motion WHERE source_article_id = %s", (article_id,))


async def delete_orphans(conn: AsyncConnection) -> Dict[str, int]:
    """After re-extraction: events no article supports any more, then entities with no stance rows and no
    affiliations (e.g. op-ed authors that are no longer extracted as actors)."""
    ev = await conn.execute(
        "DELETE FROM TB_Event ev WHERE NOT EXISTS (SELECT 1 FROM TB_Article_Event ae WHERE ae.event_id = ev.id)")
    measures = 0
    cur = await conn.execute("SELECT to_regclass('tb_measure') IS NOT NULL AS ok")
    votes_ready = (await cur.fetchone())["ok"]
    if votes_ready:  # v1.8: measures no vote and no event refers to any more
        m = await conn.execute(
            """DELETE FROM TB_Measure m
               WHERE NOT EXISTS (SELECT 1 FROM TB_Vote_Motion mo WHERE mo.measure_id = m.id)
                 AND NOT EXISTS (SELECT 1 FROM TB_Measure_Event me WHERE me.measure_id = m.id)""")
        measures = m.rowcount
    # an entity still voting or acting as a measure's body is kept (v1.8)
    in_use = """
          AND NOT EXISTS (SELECT 1 FROM TB_Vote v WHERE v.entity_id = e.id)
          AND NOT EXISTS (SELECT 1 FROM TB_Measure mm WHERE mm.body_entity_id = e.id)
          AND NOT EXISTS (SELECT 1 FROM TB_Vote_Motion vm WHERE vm.body_entity_id = e.id)""" if votes_ready else ""
    en = await conn.execute(
        """
        DELETE FROM TB_Entity e
        WHERE NOT EXISTS (SELECT 1 FROM TB_Entity_Event ee WHERE ee.entity_id = e.id)
          AND NOT EXISTS (SELECT 1 FROM TB_Entity_Affiliation a WHERE a.entity_id = e.id OR a.organization_id = e.id)
        """ + in_use
    )
    cur = await conn.execute("SELECT to_regclass('tb_entity_event_side') IS NOT NULL AS ok")
    if (await cur.fetchone())["ok"]:  # v2.0: extracted/backfilled sides nobody is on any more
        await conn.execute(
            """DELETE FROM TB_Event_Side s WHERE s.source <> 'manual'
                 AND NOT EXISTS (SELECT 1 FROM TB_Entity_Event_Side l WHERE l.side_id = s.id)""")
    return {"events": ev.rowcount, "entities": en.rowcount, "measures": measures}


async def get_source(conn: AsyncConnection, source_id: int) -> Optional[Row]:
    cur = await conn.execute("SELECT id, name, base_url, scrape_config FROM TB_Sources WHERE id = %s", (source_id,))
    return await cur.fetchone()


async def release_article(conn: AsyncConnection, article_id: int) -> None:
    """Return an IN_PROGRESS article to the queue (Stage 1 output is kept if present)."""
    await conn.execute(
        """
        UPDATE TB_Article
        SET processing_status = CASE WHEN evidence_bundle IS NULL THEN 'PENDING' ELSE 'STAGE1_DONE' END,
            claimed_at = NULL
        WHERE id = %s AND processing_status = 'IN_PROGRESS'
        """,
        (article_id,),
    )


async def release_all_claims(conn: AsyncConnection) -> int:
    """Startup recovery: a (single) worker restarted, so every IN_PROGRESS claim is stale."""
    cur = await conn.execute(
        """
        UPDATE TB_Article
        SET processing_status = CASE WHEN evidence_bundle IS NULL THEN 'PENDING' ELSE 'STAGE1_DONE' END,
            claimed_at = NULL
        WHERE processing_status = 'IN_PROGRESS'
        """
    )
    return cur.rowcount


async def queue_depth(conn: AsyncConnection) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM TB_Article WHERE processing_status IN ('PENDING', 'STAGE1_DONE')"
    )
    return (await cur.fetchone())["n"]  # type: ignore[index]


async def save_stage1(
    conn: AsyncConnection,
    article_id: int,
    *,
    resolved_text: str,
    evidence_bundle: str,
    candidate_entities: List[str],
    status: str,
) -> None:
    await conn.execute(
        """
        UPDATE TB_Article
        SET resolved_text = %s, evidence_bundle = %s, candidate_entities = %s,
            processing_status = %s, error_log = NULL,
            processed_at = CASE WHEN %s = 'SKIPPED' THEN NOW() ELSE processed_at END
        WHERE id = %s
        """,
        (resolved_text, evidence_bundle, candidate_entities, status, status, article_id),
    )


async def set_article_embedding(conn: AsyncConnection, article_id: int, embedding: Sequence[float]) -> None:
    await conn.execute(
        "UPDATE TB_Article SET embedding = %s::vector WHERE id = %s",
        (to_vector_literal(embedding), article_id),
    )


async def set_article_status(conn: AsyncConnection, article_id: int, status: str, error: Optional[str] = None) -> None:
    await conn.execute(
        """
        UPDATE TB_Article
        SET processing_status = %s, error_log = %s, claimed_at = NULL,
            processed_at = CASE WHEN %s IN ('PROCESSED', 'FAILED', 'SKIPPED') THEN NOW() ELSE processed_at END
        WHERE id = %s
        """,
        (status, error, status, article_id),
    )


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
ENTITY_MATCH_SQL = """
SELECT id, canonical_name, aliases,
       similarity(canonical_name, %(name)s) AS trgm_score,
       1 - (context_embedding <=> %(embed)s::vector) AS vec_score
FROM TB_Entity
WHERE entity_type = %(etype)s
  AND (
    LOWER(canonical_name) = LOWER(%(name)s)
    OR LOWER(%(name)s) = ANY(SELECT LOWER(unnest(aliases)))
    OR (similarity(canonical_name, %(name)s) > 0.70 AND (1 - (context_embedding <=> %(embed)s::vector)) >= 0.85)
    OR (1 - (context_embedding <=> %(embed)s::vector)) >= 0.91
  )
ORDER BY
  (CASE WHEN LOWER(canonical_name) = LOWER(%(name)s) THEN 1.0 ELSE 0.0 END) DESC,
  (similarity(canonical_name, %(name)s) * 0.4 + COALESCE(1 - (context_embedding <=> %(embed)s::vector), 0) * 0.6) DESC
LIMIT 1
"""


async def find_matching_entity(
    conn: AsyncConnection, name: str, entity_type: str, embedding: Sequence[float]
) -> Optional[Row]:
    cur = await conn.execute(
        ENTITY_MATCH_SQL,
        {"name": name, "etype": entity_type, "embed": to_vector_literal(embedding)},
    )
    return await cur.fetchone()


async def find_matching_entities(
    conn: AsyncConnection, name: str, entity_type: str, embedding: Sequence[float], limit: int = 5
) -> List[Row]:
    """Like find_matching_entity, but the best few candidates (the resolver skips incompatible ones)."""
    sql = ENTITY_MATCH_SQL.replace("LIMIT 1", "LIMIT %(limit)s")
    cur = await conn.execute(
        sql, {"name": name, "etype": entity_type, "embed": to_vector_literal(embedding), "limit": limit}
    )
    return await cur.fetchall()


async def note_unresolved_bodies(conn: AsyncConnection, article_id: int, names: Sequence[str]) -> None:
    """Remember generic body names ("Planning Commission") the resolver could not place in a city, so an admin
    can review them (Admin -> Failures). Stored in TB_Article.doc_meta.unresolved_bodies (v1.6 column)."""
    from psycopg.types.json import Jsonb

    await conn.execute(
        """UPDATE TB_Article SET doc_meta = COALESCE(doc_meta, '{}'::jsonb) || %s WHERE id = %s""",
        (Jsonb({"unresolved_bodies": sorted(set(names))}), article_id),
    )


async def add_entity_alias(conn: AsyncConnection, entity_id: int, alias: str) -> None:
    await conn.execute(
        """
        UPDATE TB_Entity
        SET aliases = array_append(aliases, %(alias)s)
        WHERE id = %(id)s
          AND LOWER(canonical_name) <> LOWER(%(alias)s)
          AND NOT (LOWER(%(alias)s) = ANY(SELECT LOWER(unnest(aliases))))
        """,
        {"id": entity_id, "alias": alias},
    )


async def insert_entity(
    conn: AsyncConnection, name: str, entity_type: str, embedding: Sequence[float]
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO TB_Entity (canonical_name, entity_type, context_embedding)
        VALUES (%s, %s, %s::vector)
        ON CONFLICT (canonical_name, entity_type) DO UPDATE
            SET context_embedding = COALESCE(TB_Entity.context_embedding, EXCLUDED.context_embedding)
        RETURNING id
        """,
        (name, entity_type, to_vector_literal(embedding)),
    )
    return (await cur.fetchone())["id"]  # type: ignore[index]


async def upsert_affiliation(
    conn: AsyncConnection,
    *,
    entity_id: int,
    organization_id: Optional[int],
    role_title: str,
    seen_at: datetime,
    source_article_id: Optional[int],
    seat: Optional[str] = None,
    with_seat: bool = False,
) -> None:
    """`with_seat` (migration v2.1 applied) also stores the seat on the body ("District 3"). An article never
    replaces a seat an admin verified, and never blanks a known one."""
    seat_cols = ", seat" if with_seat else ""
    seat_vals = ", %(seat)s" if with_seat else ""
    seat_set = """,
                seat = CASE WHEN TB_Entity_Affiliation.seat_verified_at IS NOT NULL THEN TB_Entity_Affiliation.seat
                            ELSE COALESCE(EXCLUDED.seat, TB_Entity_Affiliation.seat) END""" if with_seat else ""
    await conn.execute(
        f"""
        INSERT INTO TB_Entity_Affiliation
            (entity_id, organization_id, role_title, first_seen_date, last_seen_date, source_article_id{seat_cols})
        VALUES (%(eid)s, %(oid)s, %(role)s, %(seen)s, %(seen)s, %(aid)s{seat_vals})
        ON CONFLICT (entity_id, organization_id, role_title) DO UPDATE
            SET first_seen_date = LEAST(TB_Entity_Affiliation.first_seen_date, EXCLUDED.first_seen_date),
                last_seen_date  = GREATEST(TB_Entity_Affiliation.last_seen_date, EXCLUDED.last_seen_date),
                source_article_id = CASE
                    WHEN EXCLUDED.first_seen_date < TB_Entity_Affiliation.first_seen_date
                    THEN EXCLUDED.source_article_id
                    ELSE TB_Entity_Affiliation.source_article_id END{seat_set}
        """,
        {"eid": entity_id, "oid": organization_id, "role": role_title, "seen": seen_at, "aid": source_article_id,
         "seat": seat},
    )
    await mark_bio_dirty(conn, entity_id)


async def mark_bio_dirty(conn: AsyncConnection, entity_id: int) -> None:
    """Stage 3 trigger (SPEC_WEB_UI §4.1): entity's events/affiliations changed."""
    await conn.execute("UPDATE TB_Entity SET bio_is_dirty = TRUE WHERE id = %s", (entity_id,))


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
EVENT_CANDIDATES_SQL = """
SELECT ev.id,
       1 - (ev.embedding <=> %(embed)s::vector) AS cosine_sim,
       ARRAY_REMOVE(ARRAY_AGG(DISTINCT ee.entity_id), NULL) AS existing_entity_ids
FROM TB_Event ev
LEFT JOIN TB_Entity_Event ee ON ee.event_id = ev.id
WHERE ev.event_date BETWEEN (%(target_date)s::DATE - INTERVAL '21 days')
                        AND (%(target_date)s::DATE + INTERVAL '21 days')
GROUP BY ev.id
HAVING (1 - (ev.embedding <=> %(embed)s::vector)) >= 0.74
ORDER BY cosine_sim DESC
"""


async def find_event_candidates(
    conn: AsyncConnection, embedding: Sequence[float], target_date: date
) -> List[Row]:
    cur = await conn.execute(
        EVENT_CANDIDATES_SQL,
        {"embed": to_vector_literal(embedding), "target_date": target_date},
    )
    return await cur.fetchall()


async def insert_event(
    conn: AsyncConnection,
    *,
    canonical_title: str,
    event_type: str,
    event_date: Optional[date],
    summary: str,
    topic_tags: List[str],
    embedding: Sequence[float],
    provisional: bool = False,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO TB_Event (canonical_title, event_type, event_date, summary, topic_tags, embedding, provisional)
        VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
        RETURNING id
        """,
        (canonical_title, event_type, event_date, summary, topic_tags, to_vector_literal(embedding), provisional),
    )
    return (await cur.fetchone())["id"]  # type: ignore[index]


async def confirm_event(conn: AsyncConnection, event_id: int) -> None:
    """A non-opinion article now covers this event: it is no longer provisional."""
    await conn.execute("UPDATE TB_Event SET provisional = FALSE WHERE id = %s AND provisional", (event_id,))


async def merge_event_tags(conn: AsyncConnection, event_id: int, tags: Iterable[str]) -> None:
    await conn.execute(
        """
        UPDATE TB_Event
        SET topic_tags = ARRAY(SELECT DISTINCT t FROM unnest(topic_tags || %s::TEXT[]) AS t ORDER BY t)
        WHERE id = %s
        """,
        (list(tags), event_id),
    )


async def link_article_event(conn: AsyncConnection, article_id: int, event_id: int) -> None:
    await conn.execute(
        "INSERT INTO TB_Article_Event (article_id, event_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (article_id, event_id),
    )


async def upsert_entity_event(
    conn: AsyncConnection,
    *,
    entity_id: int,
    event_id: int,
    source_article_id: int,
    role_in_event: str,
    stance: str,
    action_summary: str,
    direct_quotes: List[str],
    action_embedding: Sequence[float],
    confidence: float = 1.0,
    evidence_type: str = "reported",
    claimed_by: Optional[str] = None,
) -> None:
    """Upsert one stance row. A disputed row keeps its disputed flag on re-extraction (admin decision wins)."""
    await conn.execute(
        """
        INSERT INTO TB_Entity_Event
            (entity_id, event_id, source_article_id, role_in_event, stance,
             action_summary, direct_quotes, action_embedding, confidence, evidence_type, claimed_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
        ON CONFLICT (entity_id, event_id, source_article_id) DO UPDATE
            SET role_in_event = EXCLUDED.role_in_event,
                stance = EXCLUDED.stance,
                action_summary = EXCLUDED.action_summary,
                direct_quotes = EXCLUDED.direct_quotes,
                action_embedding = EXCLUDED.action_embedding,
                confidence = EXCLUDED.confidence,
                evidence_type = EXCLUDED.evidence_type,
                claimed_by = EXCLUDED.claimed_by
        """,
        (
            entity_id, event_id, source_article_id, role_in_event, stance,
            action_summary, direct_quotes, to_vector_literal(action_embedding), confidence,
            evidence_type if evidence_type in ("reported", "self", "claimed") else "reported", claimed_by,
        ),
    )
    await mark_bio_dirty(conn, entity_id)


# ---------------------------------------------------------------------------
# Dossier / analytics
# ---------------------------------------------------------------------------
async def find_entity_by_name(conn: AsyncConnection, name: str) -> Optional[Row]:
    """Exact canonical/alias match first, then best trigram match above 0.3."""
    cur = await conn.execute(
        """
        SELECT id, canonical_name, entity_type, aliases, summary_bio,
               CASE
                 WHEN LOWER(canonical_name) = LOWER(%(n)s) THEN 2.0
                 WHEN LOWER(%(n)s) = ANY(SELECT LOWER(unnest(aliases))) THEN 1.5
                 ELSE similarity(canonical_name, %(n)s)
               END AS score
        FROM TB_Entity
        WHERE LOWER(canonical_name) = LOWER(%(n)s)
           OR LOWER(%(n)s) = ANY(SELECT LOWER(unnest(aliases)))
           OR similarity(canonical_name, %(n)s) > 0.3
        ORDER BY score DESC, id
        LIMIT 1
        """,
        {"n": name},
    )
    return await cur.fetchone()


async def get_affiliations(conn: AsyncConnection, entity_id: int) -> List[Row]:
    cur = await conn.execute(
        """
        SELECT a.role_title, o.canonical_name AS organization, a.first_seen_date, a.last_seen_date
        FROM TB_Entity_Affiliation a
        LEFT JOIN TB_Entity o ON o.id = a.organization_id
        WHERE a.entity_id = %s
        ORDER BY a.first_seen_date, a.last_seen_date
        """,
        (entity_id,),
    )
    return await cur.fetchall()


async def get_career_ledger(conn: AsyncConnection, entity_id: int, topic: Optional[str] = None) -> List[Row]:
    cur = await conn.execute(
        """
        SELECT * FROM VW_Entity_Career_Ledger
        WHERE entity_id = %(eid)s
          AND (%(topic)s::TEXT IS NULL OR LOWER(%(topic)s::TEXT) = ANY(topic_tags))
        ORDER BY effective_date DESC NULLS LAST, event_id DESC
        """,
        {"eid": entity_id, "topic": topic},
    )
    return await cur.fetchall()


async def get_coalition(conn: AsyncConnection, entity_id: int, *, allies: bool, limit: int = 10) -> List[Row]:
    if allies:
        sql = """
            SELECT * FROM VW_Entity_Coalition_Matrix
            WHERE entity_a_id = %s AND alignment_index > 0
            ORDER BY shared_events_count DESC, alignment_index DESC
            LIMIT %s
        """
    else:
        sql = """
            SELECT * FROM VW_Entity_Coalition_Matrix
            WHERE entity_a_id = %s AND alignment_index < 0
            ORDER BY shared_events_count DESC, alignment_index ASC
            LIMIT %s
        """
    cur = await conn.execute(sql, (entity_id, limit))
    return await cur.fetchall()


async def search_positions(
    conn: AsyncConnection, query_embedding: Sequence[float], entity_id: Optional[int] = None, limit: int = 20
) -> List[Row]:
    cur = await conn.execute(
        """
        SELECT e.canonical_name, ev.canonical_title, ev.event_date, ee.stance, ee.role_in_event,
               ee.action_summary, ee.direct_quotes, art.url AS source_url,
               1 - (ee.action_embedding <=> %(q)s::vector) AS score
        FROM TB_Entity_Event ee
        JOIN TB_Entity e ON e.id = ee.entity_id
        JOIN TB_Event ev ON ev.id = ee.event_id
        JOIN TB_Article art ON art.id = ee.source_article_id
        WHERE ee.action_embedding IS NOT NULL
          AND (%(eid)s::INT IS NULL OR ee.entity_id = %(eid)s::INT)
        ORDER BY ee.action_embedding <=> %(q)s::vector
        LIMIT %(lim)s
        """,
        {"q": to_vector_literal(query_embedding), "eid": entity_id, "lim": limit},
    )
    return await cur.fetchall()


# ---------------------------------------------------------------------------
# Worker control plane: job runs, commands, status, scheduler state
# ---------------------------------------------------------------------------
async def job_run_start(conn: AsyncConnection, job_type: str, source_id: Optional[int] = None) -> int:
    cur = await conn.execute(
        "INSERT INTO TB_Job_Run (job_type, source_id) VALUES (%s, %s) RETURNING id", (job_type, source_id)
    )
    return (await cur.fetchone())["id"]  # type: ignore[index]


async def job_run_update(
    conn: AsyncConnection, run_id: int, *, processed: int, failed: int, log_output: str
) -> None:
    await conn.execute(
        "UPDATE TB_Job_Run SET items_processed = %s, items_failed = %s, log_output = %s WHERE id = %s",
        (processed, failed, log_output, run_id),
    )


async def job_run_finish(
    conn: AsyncConnection, run_id: int, *, status: str, processed: int, failed: int,
    log_output: str, error_summary: Optional[str] = None,
) -> None:
    await conn.execute(
        """
        UPDATE TB_Job_Run
        SET status = %s, finished_at = NOW(), items_processed = %s, items_failed = %s,
            log_output = %s, error_summary = %s
        WHERE id = %s
        """,
        (status, processed, failed, log_output, error_summary, run_id),
    )


async def fail_orphaned_runs(conn: AsyncConnection) -> int:
    cur = await conn.execute(
        """
        UPDATE TB_Job_Run SET status = 'FAILED', finished_at = NOW(),
               error_summary = COALESCE(error_summary, 'Worker restarted while this job was running')
        WHERE status = 'RUNNING'
        """
    )
    return cur.rowcount


async def next_command(conn: AsyncConnection) -> Optional[Row]:
    cur = await conn.execute(
        """
        UPDATE TB_Worker_Command c SET status = 'RUNNING', started_at = NOW()
        WHERE id = (SELECT id FROM TB_Worker_Command WHERE status = 'QUEUED' ORDER BY id LIMIT 1
                    FOR UPDATE SKIP LOCKED)
        RETURNING c.*
        """
    )
    return await cur.fetchone()


async def finish_command(conn: AsyncConnection, command_id: int, status: str, result: str = "") -> None:
    await conn.execute(
        "UPDATE TB_Worker_Command SET status = %s, finished_at = NOW(), result = %s WHERE id = %s",
        (status, result[:4000], command_id),
    )


async def fail_orphaned_commands(conn: AsyncConnection) -> int:
    cur = await conn.execute(
        "UPDATE TB_Worker_Command SET status = 'FAILED', finished_at = NOW(), "
        "result = 'Worker restarted' WHERE status = 'RUNNING'"
    )
    return cur.rowcount


async def get_scheduler_state(conn: AsyncConnection) -> Row:
    cur = await conn.execute("SELECT * FROM TB_Scheduler_State WHERE id = 1")
    row = await cur.fetchone()
    return row or {"is_enabled": True, "interval_hours": 6.0, "processor_enabled": True}


_status_has_burst = True


async def write_worker_status(conn: AsyncConnection, status: Dict[str, Any]) -> None:
    import psycopg

    global _status_has_burst
    cols = ["pid", "hostname", "started_at", "crawler_state", "crawler_detail", "crawler_job_run_id",
            "processor_state", "processor_detail", "slots", "next_crawl_at", "last_crawl_finished_at"]
    if _status_has_burst:
        try:
            async with conn.transaction():
                await _upsert_status(conn, cols + ["burst"], status)
            return
        except psycopg.errors.UndefinedColumn:
            _status_has_burst = False  # v1.3 not applied yet
    await _upsert_status(conn, cols, status)


async def _upsert_status(conn: AsyncConnection, cols: List[str], status: Dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    vals = [Jsonb(status.get(c) or ({} if c == "burst" else [])) if c in ("slots", "burst") else status.get(c)
            for c in cols]
    await conn.execute(
        f"""
        INSERT INTO TB_Worker_Status (id, heartbeat_at, {", ".join(cols)})
        VALUES ('main', NOW(), {", ".join(["%s"] * len(cols))})
        ON CONFLICT (id) DO UPDATE SET heartbeat_at = NOW(),
            {", ".join(f"{c} = EXCLUDED.{c}" for c in cols)}
        """,
        vals,
    )


async def get_last_crawl_finished(conn: AsyncConnection) -> Optional[datetime]:
    cur = await conn.execute(
        "SELECT MAX(finished_at) AS t FROM TB_Job_Run WHERE job_type = 'CRAWL' AND status NOT IN ('RUNNING', 'CANCELLED')"
    )
    row = await cur.fetchone()
    return row["t"] if row else None
