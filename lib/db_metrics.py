"""Pipeline performance metrics for /admin/metrics (LLM call durations, throughput, stage timings).

TB_LLM_Call + TB_Article.stage*_ms come from migration v1.3; if it isn't applied yet the LLM-specific
parts come back empty with telemetry_missing=True (throughput still works from processed_at, v1.2).
"""
from __future__ import annotations

from typing import Any, Dict, List

import psycopg

from lib.db_conn import Db_conn

db = Db_conn()
Row = Dict[str, Any]


def _llm_calls(hours: int) -> List[Row]:
    return db.fetch_all(
        """
        SELECT c.id, c.kind, c.provider, c.cost_usd, c.thinking, c.status, c.started_at, c.duration_ms, c.concurrent_calls,
               c.prompt_tokens, c.completion_tokens, c.reasoning_tokens, c.input_chars, c.finish_reason,
               c.article_id, c.entity_id, COALESCE(a.title, e.canonical_name) AS label, s.name AS source_name
        FROM TB_LLM_Call c
        LEFT JOIN TB_Article a ON a.id = c.article_id
        LEFT JOIN TB_Sources s ON s.id = a.source_id
        LEFT JOIN TB_Entity e ON e.id = c.entity_id
        WHERE c.started_at >= NOW() - make_interval(hours => %s)
        ORDER BY c.started_at
        """,
        (hours,),
    )


def _summary(hours: int) -> Row:
    return db.fetch_one(
        """
        SELECT
          -- local server (the numbers you tune WORKER_SLOTS / thinking with)
          COUNT(*) FILTER (WHERE kind = 'STAGE2_EXTRACT' AND status = 'OK' AND provider = 'local') AS stage2_ok,
          COUNT(*) FILTER (WHERE status <> 'OK') AS not_ok,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
              FILTER (WHERE kind = 'STAGE2_EXTRACT' AND status = 'OK' AND provider = 'local') AS stage2_p50_ms,
          percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_ms)
              FILTER (WHERE kind = 'STAGE2_EXTRACT' AND status = 'OK' AND provider = 'local') AS stage2_p90_ms,
          MAX(duration_ms) FILTER (WHERE kind = 'STAGE2_EXTRACT' AND status = 'OK' AND provider = 'local') AS stage2_max_ms,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY completion_tokens * 1000.0 / NULLIF(duration_ms, 0))
              FILTER (WHERE status = 'OK' AND completion_tokens IS NOT NULL AND provider = 'local') AS tokens_per_s_p50,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
              FILTER (WHERE kind = 'STAGE3_BIO' AND status = 'OK') AS bio_p50_ms,
          -- paid API bursts
          COUNT(*) FILTER (WHERE kind = 'STAGE2_EXTRACT' AND status = 'OK' AND provider <> 'local') AS api_ok,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
              FILTER (WHERE kind = 'STAGE2_EXTRACT' AND status = 'OK' AND provider <> 'local') AS api_p50_ms,
          COALESCE(SUM(cost_usd) FILTER (WHERE provider <> 'local'), 0) AS api_spend_usd,
          AVG(cost_usd) FILTER (WHERE kind = 'STAGE2_EXTRACT' AND status = 'OK' AND provider <> 'local') AS api_cost_per_article
        FROM TB_LLM_Call
        WHERE started_at >= NOW() - make_interval(hours => %s)
        """,
        (hours,),
    ) or {}


def _by_concurrency(hours: int) -> List[Row]:
    return db.fetch_all(
        """
        SELECT concurrent_calls,
               COUNT(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY completion_tokens * 1000.0 / NULLIF(duration_ms, 0)) AS tok_s
        FROM TB_LLM_Call
        WHERE kind = 'STAGE2_EXTRACT' AND status = 'OK' AND concurrent_calls IS NOT NULL AND provider = 'local'
          AND started_at >= NOW() - make_interval(hours => %s)
        GROUP BY concurrent_calls ORDER BY concurrent_calls
        """,
        (hours,),
    )


# "local only" view: leave out articles whose extraction was done by an API burst.
NOT_VIA_API = """NOT (%(local_only)s AND EXISTS (
    SELECT 1 FROM TB_LLM_Call x
    WHERE x.article_id = TB_Article.id AND x.kind = 'STAGE2_EXTRACT' AND x.status = 'OK' AND x.provider <> 'local'))"""


def _stage_medians(hours: int, local_only: bool) -> Row:
    return db.fetch_one(
        f"""
        SELECT COUNT(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY stage1_ms) AS stage1_ms,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY llm_ms) AS llm_ms,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY resolve_ms) AS resolve_ms
        FROM TB_Article
        WHERE processing_status = 'PROCESSED' AND llm_ms IS NOT NULL
          AND processed_at >= NOW() - make_interval(hours => %(hours)s)
          AND {NOT_VIA_API}
        """,
        {"hours": hours, "local_only": local_only},
    ) or {}


def _throughput(hours: int, local_only: bool) -> List[Row]:
    return db.fetch_all(
        f"""
        SELECT date_trunc('hour', processed_at) AS hour,
               COUNT(*) FILTER (WHERE processing_status = 'PROCESSED') AS processed,
               COUNT(*) FILTER (WHERE processing_status = 'SKIPPED') AS skipped,
               COUNT(*) FILTER (WHERE processing_status = 'FAILED') AS failed
        FROM TB_Article
        WHERE processed_at >= NOW() - make_interval(hours => %(hours)s)
          AND {NOT_VIA_API}
        GROUP BY 1 ORDER BY 1
        """,
        {"hours": hours, "local_only": local_only},
    )


def _queue(local_only: bool) -> Row:
    return db.fetch_one(
        f"""
        SELECT COUNT(*) FILTER (WHERE processing_status IN ('PENDING', 'STAGE1_DONE')) AS queued,
               COUNT(*) FILTER (WHERE processing_status = 'IN_PROGRESS') AS in_progress,
               COUNT(*) FILTER (WHERE processing_status IN ('PROCESSED', 'SKIPPED')
                                AND processed_at >= NOW() - INTERVAL '6 hours' AND {NOT_VIA_API}) AS done_6h,
               COUNT(*) FILTER (WHERE processing_status IN ('PROCESSED', 'SKIPPED')
                                AND processed_at >= NOW() - INTERVAL '24 hours' AND {NOT_VIA_API}) AS done_24h
        FROM TB_Article
        """,
        {"local_only": local_only},
    ) or {}


def metrics(hours: int, local_only: bool = False) -> Row:
    """local_only=True hides everything an API burst did: its requests, its articles in throughput and
    stage timings, and its share of the queue-drain rate (so the ETA is the local server's own)."""
    out: Row = {"hours": hours, "local_only": local_only, "telemetry_missing": False}
    try:
        out.update(throughput=_throughput(hours, local_only), queue=_queue(local_only))
        calls = _llm_calls(hours)
        if local_only:
            calls = [c for c in calls if (c.get("provider") or "local") == "local"]
        out.update(calls=calls, summary=_summary(hours), by_concurrency=_by_concurrency(hours),
                   stages=_stage_medians(hours, local_only))
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        out.update(throughput=_throughput(hours, False), queue=_queue(False),
                   calls=[], summary={}, by_concurrency=[], stages={}, telemetry_missing=True)
    q = out["queue"]
    rate = (q.get("done_6h") or 0) / 6.0
    q["articles_per_hour_6h"] = rate
    q["eta_hours"] = (q["queued"] / rate) if rate > 0 else None
    return out
