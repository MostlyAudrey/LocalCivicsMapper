"""LLM call + stage timing telemetry (TB_LLM_Call, TB_Article.stage*_ms), charted on /admin/metrics.

Best-effort: telemetry must never break the pipeline. If the v1.3 tables are missing it logs one warning
and disables itself until the process restarts.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

import psycopg
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

_enabled = True
_in_flight: Dict[str, int] = {}  # LLM calls currently running in this process, per provider


class LLMCallMeta(dict):
    """Filled in by the LLM clients: model, thinking, prompt/completion/reasoning tokens, finish_reason."""


@contextmanager
def llm_call_timer(provider: str = "local") -> Iterator[Dict[str, Any]]:
    """Track wall time and in-flight concurrency (per provider) around one LLM request."""
    _in_flight[provider] = _in_flight.get(provider, 0) + 1
    t = {"started_at": datetime.now(timezone.utc), "concurrent_calls": _in_flight[provider], "_t0": time.monotonic()}
    try:
        yield t
    finally:
        _in_flight[provider] -= 1
        t["finished_at"] = datetime.now(timezone.utc)
        t["duration_ms"] = int((time.monotonic() - t["_t0"]) * 1000)


def usage_fields(resp: Any) -> Dict[str, Any]:
    u = getattr(resp, "usage", None)
    details = getattr(u, "completion_tokens_details", None) if u else None
    choice = resp.choices[0] if getattr(resp, "choices", None) else None
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", None),
        "completion_tokens": getattr(u, "completion_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None) if details else None,
        "finish_reason": getattr(choice, "finish_reason", None),
    }


async def record_llm_call(
    pool: AsyncConnectionPool, *, kind: str, timer: Dict[str, Any], meta: Optional[Dict[str, Any]],
    status: str, article_id: Optional[int] = None, entity_id: Optional[int] = None,
    input_chars: Optional[int] = None, error: Optional[str] = None,
) -> None:
    global _enabled
    if not _enabled or "duration_ms" not in timer:
        return
    m = meta or {}
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO TB_LLM_Call (kind, article_id, entity_id, model, thinking, started_at, finished_at,
                    duration_ms, concurrent_calls, input_chars, prompt_tokens, completion_tokens, reasoning_tokens,
                    finish_reason, status, error, provider, cost_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (kind, article_id, entity_id, m.get("model"), m.get("thinking"), timer["started_at"],
                 timer["finished_at"], timer["duration_ms"], m.get("concurrent_calls", timer["concurrent_calls"]),
                 input_chars, m.get("prompt_tokens"), m.get("completion_tokens"), m.get("reasoning_tokens"),
                 m.get("finish_reason"), status, (error or "")[:2000] or None, m.get("provider", "local"),
                 m.get("cost_usd") or 0),
            )
    except psycopg.errors.UndefinedTable:
        _enabled = False
        log.warning("TB_LLM_Call missing (apply sql/unapplied/v1.3_LLMTelemetry.sql); LLM telemetry disabled")
    except Exception:  # noqa: BLE001
        log.exception("could not record LLM call telemetry")


async def record_stage_timings(pool: AsyncConnectionPool, article_id: int, **ms: Optional[int]) -> None:
    global _enabled
    if not _enabled:
        return
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE TB_Article SET stage1_ms = COALESCE(%s, stage1_ms), llm_ms = %s, resolve_ms = %s WHERE id = %s",
                (ms.get("stage1_ms"), ms.get("llm_ms"), ms.get("resolve_ms"), article_id),
            )
    except psycopg.errors.UndefinedColumn:
        _enabled = False
        log.warning("TB_Article stage timing columns missing (apply v1.3); telemetry disabled")
    except Exception:  # noqa: BLE001
        log.exception("could not record stage timings")
