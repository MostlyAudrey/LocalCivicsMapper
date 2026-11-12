"""Admin-side queries: worker control plane, telemetry, sources, failures, entity merge."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from psycopg import sql
from psycopg.types.json import Jsonb

from lib import affiliations, seats
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
               COUNT(a.id) FILTER (WHERE a.processing_status = 'SKIPPED') AS skipped,
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


def merge_entities(source_id: int, target_id: int, assign_seat: bool = True) -> Row:
    """Merge source into target (SPEC_WEB_UI §7.4). Target keeps its canonical name; the source's name and
    aliases become target aliases. Conflicting rows are merged (quotes unioned, date ranges widened).

    When the source is a seat saved as a body ("<body> District 3"), its people get that seat on the target
    (unless `assign_seat` is False - use that when the seat entity collected people from other seats too), and
    its seat-like names are not kept as aliases."""
    if source_id == target_id:
        raise ValueError("source and target are the same entity")
    with db.transaction() as conn:
        with conn.transaction():
            src = conn.execute("SELECT * FROM TB_Entity WHERE id = %s FOR UPDATE", (source_id,)).fetchone()
            tgt = conn.execute("SELECT * FROM TB_Entity WHERE id = %s FOR UPDATE", (target_id,)).fetchone()
            if not src or not tgt:
                raise ValueError("entity not found")

            # aliases: target ∪ source name ∪ source aliases, minus target's own name (case-insensitive).
            # A seat or office title ("... District 3", "County Supervisor") is not a name of the body: as an
            # alias it would pull every other body's District 3 here, so it is left out.
            is_seat = seats.looks_like_seat_entity(src["canonical_name"])
            seat_like = [n for n in [src["canonical_name"], *(src["aliases"] or [])]
                         if seats.looks_like_seat_entity(n) or (is_seat and seats.normalize_seat(n))]
            src_aliases = [n for n in (src["aliases"] or []) if n not in seat_like]
            src_names = [] if src["canonical_name"] in seat_like else [src["canonical_name"]]
            has_seat = conn.execute(
                """SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'tb_entity_affiliation'
                                  AND column_name = 'seat') AS ok""").fetchone()["ok"]
            src_seat = seats.normalize_seat(src["canonical_name"]) if is_seat and assign_seat else None
            if has_seat and src_seat:  # "<body> District 3" merged into the body: its people hold that seat
                conn.execute("UPDATE TB_Entity_Affiliation SET seat = %s WHERE organization_id = %s AND seat IS NULL",
                             (src_seat, source_id))
            conn.execute(
                """
                UPDATE TB_Entity t SET aliases = ARRAY(
                    SELECT DISTINCT ON (LOWER(x)) x
                    FROM unnest(t.aliases || %(snames)s::TEXT[] || %(saliases)s::TEXT[]) x
                    WHERE LOWER(x) <> LOWER(t.canonical_name)
                ), bio_is_dirty = TRUE
                WHERE t.id = %(tid)s
                """,
                {"snames": src_names, "saliases": src_aliases, "tid": target_id},
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
            if conn.execute("SELECT to_regclass('tb_entity_event_side') IS NOT NULL AS ok").fetchone()["ok"]:
                conn.execute(  # v2.0: the duplicate row's sides move to the row it merges into
                    """
                    INSERT INTO TB_Entity_Event_Side (entity_event_id, side_id)
                    SELECT t.id, l.side_id FROM TB_Entity_Event s
                    JOIN TB_Entity_Event t ON t.entity_id = %(tid)s AND t.event_id = s.event_id
                         AND t.source_article_id = s.source_article_id
                    JOIN TB_Entity_Event_Side l ON l.entity_event_id = s.id
                    WHERE s.entity_id = %(sid)s
                    ON CONFLICT DO NOTHING
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
                if has_seat:  # the surviving row keeps a seat either row knew; a verified one wins
                    conn.execute(sql.SQL(
                        """
                        UPDATE TB_Entity_Affiliation t
                        SET seat = CASE WHEN t.seat_verified_at IS NOT NULL THEN t.seat
                                        WHEN s.seat_verified_at IS NOT NULL THEN s.seat
                                        ELSE COALESCE(t.seat, s.seat) END,
                            seat_verified_at = COALESCE(t.seat_verified_at, s.seat_verified_at),
                            seat_verified_source = COALESCE(t.seat_verified_source, s.seat_verified_source)
                        FROM TB_Entity_Affiliation s
                        WHERE s.{c} = %(sid)s AND t.{c} = %(tid)s
                          AND t.{o} IS NOT DISTINCT FROM s.{o} AND t.role_title = s.role_title
                        """).format(c=c, o=o), ids)
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


# ---------------------------------------------------------------- seats (v2.1)
SEAT_ROLE_SQL = r"(supervisor|council|trustee|mayor|board\s+member|director)"   # roles elected to a seat
SEAT_HOLDER_YEARS = 2


def seats_ready() -> bool:
    row = db.fetch_one(
        """SELECT EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_name = 'tb_entity_affiliation' AND column_name = 'seat') AS ok""")
    return bool(row and row["ok"])


def seat_holders(q: str = "", limit: int = 300) -> List[Row]:
    """One line per (person, body) for people recently recorded in an elected seat of a government body:
    their roles, the seat on file and whether an admin verified it. Unverified first, grouped by body."""
    if not seats_ready():
        return []
    rows = db.fetch_all(
        """
        SELECT p.id AS person_id, p.canonical_name AS person, o.id AS organization_id, o.canonical_name AS body,
               array_agg(DISTINCT a.role_title) AS roles, max(a.last_seen_date) AS last_seen,
               (array_agg(a.seat ORDER BY (a.seat_verified_at IS NOT NULL) DESC, a.last_seen_date DESC)
                    FILTER (WHERE a.seat IS NOT NULL))[1] AS seat,
               max(a.seat_verified_at) AS verified_at, max(a.seat_verified_source) AS verified_source
        FROM TB_Entity_Affiliation a
        JOIN TB_Entity p ON p.id = a.entity_id AND p.entity_type = 'PERSON'
        JOIN TB_Entity o ON o.id = a.organization_id AND o.entity_type = 'GOVERNMENT_BODY'
        WHERE a.role_title ~* %(roles)s AND a.role_title !~* '(candidate|former)'
          AND (%(q)s = '' OR o.canonical_name ILIKE %(like)s OR p.canonical_name ILIKE %(like)s)
        GROUP BY p.id, p.canonical_name, o.id, o.canonical_name
        HAVING max(a.last_seen_date) > NOW() - make_interval(years => %(years)s)
        ORDER BY (max(a.seat_verified_at) IS NOT NULL), o.canonical_name, p.canonical_name
        LIMIT %(limit)s
        """,
        {"roles": SEAT_ROLE_SQL, "q": q.strip(), "like": f"%{q.strip()}%", "years": SEAT_HOLDER_YEARS,
         "limit": limit},
    )
    for r in rows:
        labels = []
        for t in r["roles"]:
            label = affiliations.role_family(t, r["body"])[1]
            if label not in labels:
                labels.append(label)
        r["role"] = ", ".join(labels)
    return rows


def verify_seat(person_id: int, organization_id: int, seat: str, source: str) -> int:
    """Record an admin's check of someone's seat on a body. `seat` is normalized ("district three" ->
    "District 3"); empty means "no numbered seat" (at-large). The seat is set on all the person's rows on that
    body and is never overwritten by article extraction afterwards. Returns the number of rows updated."""
    if not seats_ready():
        raise ValueError("apply sql/unapplied/v2.1_AffiliationSeat.sql first")
    seat = " ".join((seat or "").split())
    norm = seats.normalize_seat(seat) if seat else None
    if seat and not norm:
        raise ValueError(f"“{seat}” is not a seat like “District 3”, “Ward 2” or “Area 4”")
    source = " ".join((source or "").split())[:300]
    if not source:
        raise ValueError("say where you checked it (a link or a short note)")
    with db.transaction() as conn:
        n = conn.execute(
            """UPDATE TB_Entity_Affiliation SET seat = %s, seat_verified_at = NOW(), seat_verified_source = %s
               WHERE entity_id = %s AND organization_id = %s""",
            (norm, source, person_id, organization_id)).rowcount
        conn.execute("UPDATE TB_Entity SET bio_is_dirty = TRUE WHERE id IN (%s, %s)", (person_id, organization_id))
    return n


def unverify_seat(person_id: int, organization_id: int) -> int:
    """Undo a verification (the seat value stays; articles may update it again)."""
    return db.execute(
        """UPDATE TB_Entity_Affiliation SET seat_verified_at = NULL, seat_verified_source = NULL
           WHERE entity_id = %s AND organization_id = %s""", (person_id, organization_id))


def seat_like_entities(limit: int = 200) -> List[Row]:
    """Audit: bodies/organisations whose NAME is a seat or an office title ("... District 3", "County
    Supervisor"). They should be merged into the real body; the merge moves the seat onto the affiliations."""
    rows = db.fetch_all(
        r"""
        SELECT e.id, e.canonical_name, e.entity_type, e.created_at,
               (SELECT count(*) FROM TB_Entity_Affiliation a WHERE a.organization_id = e.id) AS n_affs,
               (SELECT count(*) FROM TB_Entity_Event ee WHERE ee.entity_id = e.id) AS n_events
        FROM TB_Entity e
        WHERE e.entity_type IN ('GOVERNMENT_BODY', 'ORGANIZATION')
          AND (e.canonical_name ~* '(district|ward|area|seat|division)\s+(no\.?\s*|#\s*)?([0-9]+|[a-h]|one|two|three|four|five|six|seven)\M'
               OR e.canonical_name ~* '^(the\s+)?((county|city)\s+)?(supervisor|council\s*(member|man|woman|person))s?$')
        ORDER BY e.created_at DESC
        """)
    return [r for r in rows if seats.looks_like_seat_entity(r["canonical_name"])][:limit]
