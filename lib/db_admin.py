"""Admin-side queries: worker control plane, telemetry, sources, failures, entity merge."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from psycopg import sql
from psycopg.types.json import Jsonb

from lib.db_conn import Db_conn

db = Db_conn()
Row = Dict[str, Any]

COMMANDS = {"CRAWL_NOW", "BACKFILL", "STOP_CRAWL", "CANCEL_PROCESSING", "SYNTHESIZE_BIOS", "REGENERATE_BIO",
            "BURST", "STOP_BURST"}
WORKER_STALE_SECONDS = 20


# ---------------------------------------------------------------- dashboard
def worker_status() -> Optional[Row]:
    return db.fetch_one(
        """SELECT *, EXTRACT(EPOCH FROM (NOW() - heartbeat_at)) AS heartbeat_age,
                  EXTRACT(EPOCH FROM (NOW() - started_at)) AS uptime
           FROM TB_Worker_Status WHERE id = 'main'"""
    )


def scheduler_state() -> Row:
    return db.fetch_one("SELECT * FROM TB_Scheduler_State WHERE id = 1") or {
        "is_enabled": True, "interval_hours": 6.0, "processor_enabled": True}


def update_scheduler(is_enabled: Optional[bool] = None, interval_hours: Optional[float] = None,
                     processor_enabled: Optional[bool] = None) -> Row:
    db.execute(
        """
        UPDATE TB_Scheduler_State SET
            is_enabled = COALESCE(%s, is_enabled),
            interval_hours = COALESCE(%s, interval_hours),
            processor_enabled = COALESCE(%s, processor_enabled),
            updated_at = NOW()
        WHERE id = 1
        """,
        (is_enabled, interval_hours, processor_enabled),
    )
    return scheduler_state()


def counters() -> Row:
    row = _counters()
    if row:
        row["bios_not_ready"] = (row.get("dirty_bios") or 0) - (row.get("bios_waiting") or 0)
    return row


def _counters() -> Row:
    return db.fetch_one(
        """
        SELECT (SELECT COUNT(*) FROM TB_Sources) AS sources,
               (SELECT COUNT(*) FROM TB_Sources WHERE is_active) AS active_sources,
               COUNT(*) FILTER (WHERE processing_status = 'PENDING') AS pending,
               COUNT(*) FILTER (WHERE processing_status = 'STAGE1_DONE') AS stage1_done,
               COUNT(*) FILTER (WHERE processing_status = 'IN_PROGRESS') AS in_progress,
               COUNT(*) FILTER (WHERE processing_status = 'PROCESSED') AS processed,
               COUNT(*) FILTER (WHERE processing_status = 'FAILED') AS failed,
               COUNT(*) FILTER (WHERE processing_status = 'SKIPPED') AS skipped,
               COUNT(*) AS articles,
               (SELECT COUNT(*) FROM TB_Entity) AS entities,
               (SELECT COUNT(*) FROM TB_Entity WHERE bio_is_dirty) AS dirty_bios,
               -- dirty AND eligible for a bio: mirrors civic_tracker stage3_wiki ELIGIBLE_DIRTY_SQL
               -- (>= 3 events, or >= 2 events plus an affiliation); keep the two in sync
               (SELECT COUNT(*) FROM TB_Entity e
                 WHERE e.bio_is_dirty
                   AND ((SELECT COUNT(DISTINCT ee.event_id) FROM TB_Entity_Event ee WHERE ee.entity_id = e.id) >= 3
                        OR ((SELECT COUNT(DISTINCT ee.event_id) FROM TB_Entity_Event ee WHERE ee.entity_id = e.id) >= 2
                            AND EXISTS (SELECT 1 FROM TB_Entity_Affiliation a WHERE a.entity_id = e.id)))
               ) AS bios_waiting,
               (SELECT COUNT(*) FROM TB_Entity WHERE summary_bio IS NOT NULL) AS bios,
               (SELECT COUNT(*) FROM TB_Event) AS events
        FROM TB_Article
        """
    ) or {}


def queue_by_source() -> List[Row]:
    return db.fetch_all(
        """
        SELECT s.id, s.name, s.is_active, s.last_crawled_at,
               COUNT(a.id) FILTER (WHERE a.processing_status IN ('PENDING', 'STAGE1_DONE')) AS queued,
               COUNT(a.id) FILTER (WHERE a.processing_status = 'PROCESSED') AS processed,
               COUNT(a.id) FILTER (WHERE a.processing_status = 'FAILED') AS failed,
               MAX(a.published_at) AS newest
        FROM TB_Sources s LEFT JOIN TB_Article a ON a.source_id = s.id
        GROUP BY s.id ORDER BY s.id
        """
    )


def recent_job_runs(limit: int = 15) -> List[Row]:
    return db.fetch_all(
        """
        SELECT r.id, r.job_type, r.status, r.started_at, r.finished_at, r.items_processed, r.items_failed,
               r.error_summary, s.name AS source_name,
               EXTRACT(EPOCH FROM (COALESCE(r.finished_at, NOW()) - r.started_at)) AS duration
        FROM TB_Job_Run r LEFT JOIN TB_Sources s ON s.id = r.source_id
        ORDER BY r.id DESC LIMIT %s
        """,
        (limit,),
    )


def job_run(run_id: int) -> Optional[Row]:
    return db.fetch_one("SELECT * FROM TB_Job_Run WHERE id = %s", (run_id,))


def open_alerts() -> List[Row]:
    return db.fetch_all(
        """SELECT id, job_type, status, finished_at, items_processed, items_failed, error_summary
           FROM TB_Job_Run WHERE status IN ('FAILED', 'PARTIAL_FAILURE') AND NOT alert_acknowledged
           ORDER BY id DESC LIMIT 10"""
    )


def acknowledge_alerts() -> int:
    return db.execute("UPDATE TB_Job_Run SET alert_acknowledged = TRUE WHERE NOT alert_acknowledged")


def recent_commands(limit: int = 8) -> List[Row]:
    return db.fetch_all("SELECT * FROM TB_Worker_Command ORDER BY id DESC LIMIT %s", (limit,))


def enqueue_command(command: str, params: Optional[Dict[str, Any]] = None) -> int:
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command}")
    row = db.fetch_one(
        "INSERT INTO TB_Worker_Command (command, params) VALUES (%s, %s) RETURNING id",
        (command, Jsonb(params or {})),
    )
    return row["id"]  # type: ignore[index]


# ---------------------------------------------------------------- sources
def list_sources() -> List[Row]:
    return db.fetch_all(
        """
        SELECT s.*, COUNT(a.id) AS n_articles,
               COUNT(a.id) FILTER (WHERE a.processing_status IN ('PENDING', 'STAGE1_DONE')) AS queued
        FROM TB_Sources s LEFT JOIN TB_Article a ON a.source_id = s.id
        GROUP BY s.id ORDER BY s.id
        """
    )


def get_source(source_id: int) -> Optional[Row]:
    return db.fetch_one("SELECT * FROM TB_Sources WHERE id = %s", (source_id,))


def save_source(source_id: Optional[int], *, name: str, base_url: str, sitemap_url: Optional[str],
                rss_url: Optional[str], is_active: bool, scrape_config: Dict[str, Any],
                reliability: float = 1.0) -> int:
    """reliability (0-1) scales the weight of everything this source says (see v1.5 weighting)."""
    if not 0 <= reliability <= 1:
        raise ValueError("reliability must be between 0 and 1")
    if source_id:
        db.execute(
            """UPDATE TB_Sources SET name = %s, base_url = %s, sitemap_url = %s, rss_url = %s, is_active = %s,
                      scrape_config = %s, reliability = %s WHERE id = %s""",
            (name, base_url.rstrip("/"), sitemap_url, rss_url, is_active, Jsonb(scrape_config), reliability,
             source_id),
        )
        return source_id
    row = db.fetch_one(
        """INSERT INTO TB_Sources (name, base_url, sitemap_url, rss_url, is_active, scrape_config, reliability)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (name, base_url.rstrip("/"), sitemap_url, rss_url, is_active, Jsonb(scrape_config), reliability),
    )
    return row["id"]  # type: ignore[index]


def toggle_source(source_id: int) -> None:
    db.execute("UPDATE TB_Sources SET is_active = NOT is_active WHERE id = %s", (source_id,))


def parse_config_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    cfg = json.loads(text)
    if not isinstance(cfg, dict):
        raise ValueError("scrape_config must be a JSON object")
    return cfg


# ---------------------------------------------------------------- failures
def failed_articles(limit: int = 200) -> List[Row]:
    return db.fetch_all(
        """
        SELECT a.id, a.title, a.url, a.scraped_at, a.processed_at, a.error_log, s.name AS source_name
        FROM TB_Article a JOIN TB_Sources s ON s.id = a.source_id
        WHERE a.processing_status = 'FAILED'
        ORDER BY a.processed_at DESC NULLS LAST, a.id DESC LIMIT %s
        """,
        (limit,),
    )


def retry_articles(ids: Optional[List[int]]) -> int:
    """FAILED -> PENDING (or STAGE1_DONE if Stage 1 output exists). ids=None retries all."""
    return db.execute(
        """
        UPDATE TB_Article
        SET processing_status = CASE WHEN evidence_bundle IS NULL THEN 'PENDING' ELSE 'STAGE1_DONE' END,
            error_log = NULL
        WHERE processing_status = 'FAILED' AND (%(all)s OR id = ANY(%(ids)s))
        """,
        {"all": ids is None, "ids": ids or []},
    )


def triage_skipped(limit: int = 200) -> List[Row]:
    """Articles triage skipped (not local / not news), newest first, with the reason."""
    return db.fetch_all(
        """
        SELECT a.id, a.title, a.url, a.published_at, a.processed_at, a.skip_reason, a.genre, a.yolo_relevance,
               s.name AS source_name
        FROM TB_Article a JOIN TB_Sources s ON s.id = a.source_id
        WHERE a.processing_status = 'SKIPPED' AND a.skip_reason IS NOT NULL AND a.skip_reason <> 'override'
        ORDER BY a.processed_at DESC NULLS LAST, a.id DESC LIMIT %s
        """,
        (limit,),
    )


def process_anyway(ids: List[int]) -> int:
    """Admin override: re-queue triage-skipped articles; triage is bypassed for them (skip_reason='override')."""
    return db.execute(
        """
        UPDATE TB_Article
        SET processing_status = CASE WHEN evidence_bundle IS NULL THEN 'PENDING' ELSE 'STAGE1_DONE' END,
            skip_reason = 'override', processed_at = NULL
        WHERE processing_status = 'SKIPPED' AND id = ANY(%s)
        """,
        (ids,),
    )


def skip_articles(ids: List[int]) -> int:
    return db.execute(
        "UPDATE TB_Article SET processing_status = 'SKIPPED', processed_at = NOW() "
        "WHERE processing_status = 'FAILED' AND id = ANY(%s)",
        (ids,),
    )


# ---------------------------------------------------------------- entities
def search_entities(q: str, limit: int = 15) -> List[Row]:
    return db.fetch_all(
        """
        SELECT e.id, e.canonical_name, e.entity_type, e.aliases,
               (SELECT COUNT(DISTINCT event_id) FROM TB_Entity_Event WHERE entity_id = e.id) AS n_events
        FROM TB_Entity e
        WHERE e.canonical_name ILIKE %(like)s OR e.canonical_name %% %(q)s
           OR EXISTS (SELECT 1 FROM unnest(e.aliases) a WHERE a ILIKE %(like)s)
        ORDER BY similarity(e.canonical_name, %(q)s) DESC LIMIT %(lim)s
        """,
        {"q": q, "like": f"%{q}%", "lim": limit},
    )


def not_duplicate_table_ready() -> bool:
    """True once migration v1.7 (TB_Entity_Not_Duplicate) is applied."""
    row = db.fetch_one("SELECT to_regclass('tb_entity_not_duplicate') IS NOT NULL AS ok")
    return bool(row and row["ok"])


def possible_duplicates(limit: int = 40) -> List[Row]:
    """Same-type entity pairs with similar names - candidates for manual merge - minus pairs an admin
    marked "not duplicates" (v1.7). Each side carries its event count so the admin can pick which to keep."""
    exclude = ""
    if not_duplicate_table_ready():
        exclude = """AND NOT EXISTS (SELECT 1 FROM TB_Entity_Not_Duplicate nd
                                    WHERE nd.entity_a_id = a.id AND nd.entity_b_id = b.id)"""
    return db.fetch_all(
        """
        SELECT a.id AS a_id, a.canonical_name AS a_name, b.id AS b_id, b.canonical_name AS b_name,
               a.entity_type, ROUND(similarity(a.canonical_name, b.canonical_name)::NUMERIC, 2) AS sim,
               (SELECT COUNT(DISTINCT event_id) FROM TB_Entity_Event WHERE entity_id = a.id) AS a_events,
               (SELECT COUNT(DISTINCT event_id) FROM TB_Entity_Event WHERE entity_id = b.id) AS b_events
        FROM TB_Entity a JOIN TB_Entity b
          ON a.id < b.id AND a.entity_type = b.entity_type AND a.canonical_name %% b.canonical_name
        WHERE TRUE """ + exclude + """
        ORDER BY sim DESC LIMIT %s
        """,
        (limit,),
    )


def remove_entity_alias(entity_id: int, alias: str) -> Optional[Row]:
    """Drop one alias (case-insensitive). The bio is flagged so it stops using the name."""
    return db.fetch_one(
        """UPDATE TB_Entity SET aliases = ARRAY(SELECT a FROM unnest(aliases) a WHERE LOWER(a) <> LOWER(%s)),
                  bio_is_dirty = TRUE
           WHERE id = %s RETURNING id, aliases""",
        (alias.strip(), entity_id),
    )


def add_entity_alias(entity_id: int, alias: str) -> Optional[Row]:
    """Add an alias, unless it's empty, too long, the entity's own name, or already another entity's name/alias
    of the same type (that would make the importer's matching ambiguous - merge those entities instead)."""
    alias = " ".join(alias.split())
    if not alias or len(alias) > 120:
        raise ValueError("alias must be 1-120 characters")
    clash = db.fetch_one(
        """SELECT o.id, o.canonical_name FROM TB_Entity e JOIN TB_Entity o
             ON o.entity_type = e.entity_type AND o.id <> e.id
           WHERE e.id = %(id)s AND (LOWER(o.canonical_name) = LOWER(%(a)s)
                 OR LOWER(%(a)s) = ANY(SELECT LOWER(unnest(o.aliases))))
           LIMIT 1""",
        {"id": entity_id, "a": alias},
    )
    if clash:
        raise ValueError(f"“{alias}” already names #{clash['id']} {clash['canonical_name']} - merge them instead")
    return db.fetch_one(
        """UPDATE TB_Entity SET aliases = CASE
               WHEN LOWER(%(a)s) = LOWER(canonical_name) OR LOWER(%(a)s) = ANY(SELECT LOWER(unnest(aliases)))
               THEN aliases ELSE array_append(aliases, %(a)s) END
           WHERE id = %(id)s RETURNING id, aliases""",
        {"a": alias, "id": entity_id},
    )


def dismiss_duplicate(id1: int, id2: int) -> None:
    """Remember that two entities are NOT the same, so the pair is never suggested again (needs v1.7)."""
    if id1 == id2:
        raise ValueError("same entity")
    a, b = sorted((int(id1), int(id2)))
    db.execute(
        """INSERT INTO TB_Entity_Not_Duplicate (entity_a_id, entity_b_id) VALUES (%s, %s)
           ON CONFLICT DO NOTHING""",
        (a, b),
    )


def set_dispute(entity_event_id: int, disputed: bool, note: Optional[str] = None) -> Optional[Row]:
    """Dispute (hide) or restore one stance record. Disputed records drop out of every view (coalitions,
    ledgers, salience) and bios; the entity's bio is flagged for regeneration either way."""
    with db.transaction() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                UPDATE TB_Entity_Event
                SET disputed = %(d)s,
                    dispute_note = CASE WHEN %(d)s THEN %(note)s ELSE NULL END,
                    disputed_at = CASE WHEN %(d)s THEN NOW() ELSE NULL END
                WHERE id = %(id)s
                RETURNING id, entity_id, event_id, disputed
                """,
                {"id": entity_event_id, "d": disputed, "note": (note or "").strip()[:1000] or None},
            ).fetchone()
            if row:
                conn.execute("UPDATE TB_Entity SET bio_is_dirty = TRUE WHERE id = %s", (row["entity_id"],))
    return row


def merge_entities(source_id: int, target_id: int) -> Row:
    """Merge source into target (SPEC_WEB_UI §7.4). Target keeps its canonical name; the source's name and
    aliases become target aliases. Conflicting rows are merged (quotes unioned, date ranges widened)."""
    if source_id == target_id:
        raise ValueError("source and target are the same entity")
    with db.transaction() as conn:
        with conn.transaction():
            src = conn.execute("SELECT * FROM TB_Entity WHERE id = %s FOR UPDATE", (source_id,)).fetchone()
            tgt = conn.execute("SELECT * FROM TB_Entity WHERE id = %s FOR UPDATE", (target_id,)).fetchone()
            if not src or not tgt:
                raise ValueError("entity not found")

            # aliases: target ∪ source name ∪ source aliases, minus target's own name (case-insensitive)
            conn.execute(
                """
                UPDATE TB_Entity t SET aliases = ARRAY(
                    SELECT DISTINCT ON (LOWER(x)) x
                    FROM unnest(t.aliases || ARRAY[%(sname)s]::TEXT[] || %(saliases)s::TEXT[]) x
                    WHERE LOWER(x) <> LOWER(t.canonical_name)
                ), bio_is_dirty = TRUE
                WHERE t.id = %(tid)s
                """,
                {"sname": src["canonical_name"], "saliases": src["aliases"] or [], "tid": target_id},
            )

            # entity-event rows: merge into an existing target row for the same (event, article), else re-point
            conn.execute(
                """
                UPDATE TB_Entity_Event t SET direct_quotes = ARRAY(
                    SELECT DISTINCT q FROM unnest(t.direct_quotes || s.direct_quotes) q)
                FROM TB_Entity_Event s
                WHERE s.entity_id = %(sid)s AND t.entity_id = %(tid)s
                  AND t.event_id = s.event_id AND t.source_article_id = s.source_article_id
                """,
                {"sid": source_id, "tid": target_id},
            )
            conn.execute(
                """
                DELETE FROM TB_Entity_Event s USING TB_Entity_Event t
                WHERE s.entity_id = %(sid)s AND t.entity_id = %(tid)s
                  AND t.event_id = s.event_id AND t.source_article_id = s.source_article_id
                """,
                {"sid": source_id, "tid": target_id},
            )
            moved_events = conn.execute(
                "UPDATE TB_Entity_Event SET entity_id = %s WHERE entity_id = %s", (target_id, source_id)).rowcount

            # affiliations where the source is the person/member, then where it is the organization
            for col, other in (("entity_id", "organization_id"), ("organization_id", "entity_id")):
                ids = {"sid": source_id, "tid": target_id}
                c, o = sql.Identifier(col), sql.Identifier(other)
                conn.execute(sql.SQL(
                    """
                    UPDATE TB_Entity_Affiliation t
                    SET first_seen_date = LEAST(t.first_seen_date, s.first_seen_date),
                        last_seen_date = GREATEST(t.last_seen_date, s.last_seen_date)
                    FROM TB_Entity_Affiliation s
                    WHERE s.{c} = %(sid)s AND t.{c} = %(tid)s
                      AND t.{o} IS NOT DISTINCT FROM s.{o} AND t.role_title = s.role_title
                    """).format(c=c, o=o), ids)
                conn.execute(sql.SQL(
                    """
                    DELETE FROM TB_Entity_Affiliation s USING TB_Entity_Affiliation t
                    WHERE s.{c} = %(sid)s AND t.{c} = %(tid)s
                      AND t.{o} IS NOT DISTINCT FROM s.{o} AND t.role_title = s.role_title
                    """).format(c=c, o=o), ids)
                conn.execute(sql.SQL("UPDATE TB_Entity_Affiliation SET {c} = %(tid)s WHERE {c} = %(sid)s")
                             .format(c=c), ids)
            # a person affiliated with itself after merging (A member of B, A merged into B) is meaningless
            conn.execute("DELETE FROM TB_Entity_Affiliation WHERE entity_id = organization_id")

            # v1.8 measures/votes reference entities with ON DELETE SET NULL: repoint them, or the merge would
            # strip members from their ballots and bodies from their measures
            if conn.execute("SELECT to_regclass('tb_vote_motion') IS NOT NULL AS ok").fetchone()["ok"]:
                ids = {"sid": source_id, "tid": target_id}
                conn.execute("UPDATE TB_Vote SET entity_id = %(tid)s WHERE entity_id = %(sid)s", ids)
                conn.execute("UPDATE TB_Vote_Motion SET body_entity_id = %(tid)s WHERE body_entity_id = %(sid)s", ids)
                conn.execute("UPDATE TB_Measure SET body_entity_id = %(tid)s WHERE body_entity_id = %(sid)s", ids)

            conn.execute("DELETE FROM TB_Entity WHERE id = %s", (source_id,))
    return {"target_id": target_id, "source_name": src["canonical_name"], "target_name": tgt["canonical_name"],
            "moved_event_rows": moved_events}
