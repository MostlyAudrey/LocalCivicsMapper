"""Webhook failure alerts (SPEC_WEB_UI §7). JSON body {"text": ...} works for Mattermost, Slack,
Discord (via /slack-compatible URL) and Home Assistant webhooks."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from civic_tracker.config import settings

log = logging.getLogger(__name__)


async def send_job_alert(job_type: str, run_id: int, status: str, processed: int, failed: int,
                         summary: Optional[str]) -> None:
    if not settings.ALERT_WEBHOOK_URL:
        return
    text = (f"🚨 **Civic Tracker Alert**: Job {job_type} #{run_id} finished {status} "
            f"({processed} ok, {failed} failed)." + (f"\n{summary[:500]}" if summary else ""))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(settings.ALERT_WEBHOOK_URL, json={"text": text})
            r.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - alerting must never break the worker
        log.warning("alert webhook failed: %s", exc)
