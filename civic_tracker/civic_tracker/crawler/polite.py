"""Polite HTTP client used for ALL crawler traffic.

These are small local newspapers, so every request a crawl makes (robots.txt, sitemaps, API
pages, listing pages, articles) goes through one client per source that:
  * spaces requests at least `delay` seconds apart (raised to the site's robots.txt Crawl-delay),
    plus a random extra (up to CRAWL_JITTER_FACTOR x delay) and, now and then, a long random break,
    so the load on a site is lighter and spread out. Never shorter than `delay`; the User-Agent stays honest,
  * on 429/503 honours Retry-After (else exponential backoff from 60s, capped at 10 min),
  * gives up on the source for this run after `max_throttles` consecutive throttles.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple

import httpx

from civic_tracker.config import settings

log = logging.getLogger(__name__)

THROTTLE_STATUSES = (429, 503)
BACKOFF_START_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 600.0


class SourceThrottled(Exception):
    """The site kept rate-limiting us; stop crawling it for this run."""


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class PoliteClient(httpx.AsyncClient):
    def __init__(self, delay: float, max_throttles: int = 3, *,
                 jitter_factor: Optional[float] = None, pause_chance: Optional[float] = None,
                 pause_range: Optional[Tuple[float, float]] = None, **kwargs) -> None:
        kwargs.setdefault("headers", {"User-Agent": settings.USER_AGENT})
        kwargs.setdefault("timeout", settings.CRAWL_TIMEOUT_SECONDS)
        kwargs.setdefault("follow_redirects", True)
        super().__init__(**kwargs)
        self.delay = max(0.0, delay)
        self.max_throttles = max_throttles
        self.requests_made = 0
        self.throttle_events = 0
        self._consecutive_throttles = 0
        self._next_ok = 0.0
        self._lock = asyncio.Lock()
        self.jitter_factor = max(0.0, settings.CRAWL_JITTER_FACTOR if jitter_factor is None else jitter_factor)
        self.pause_chance = settings.CRAWL_PAUSE_CHANCE if pause_chance is None else pause_chance
        self.pause_range = pause_range or (settings.CRAWL_PAUSE_MIN_SECONDS, settings.CRAWL_PAUSE_MAX_SECONDS)
        self.pauses_taken = 0
        self._rng = random.Random()

    def next_gap(self) -> float:
        """Seconds until the next request: delay + random extra, sometimes a long break. Never below delay."""
        if self.delay <= 0:
            return 0.0  # unthrottled clients (tests, local fixtures)
        gap = self.delay * (1.0 + self._rng.uniform(0.0, self.jitter_factor))
        if self.pause_chance > 0 and self._rng.random() < self.pause_chance:
            lo, hi = self.pause_range
            pause = self._rng.uniform(lo, max(lo, hi))
            self.pauses_taken += 1
            log.info("crawl: taking a %.0fs break before the next request", pause)
            gap += pause
        return gap

    def raise_delay(self, seconds: Optional[float]) -> None:
        if seconds and seconds > self.delay:
            log.info("raising crawl delay %.1fs -> %.1fs (robots.txt Crawl-delay)", self.delay, seconds)
            self.delay = float(seconds)

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:  # type: ignore[override]
        async with self._lock:  # strictly one request at a time per source
            attempt = 0
            while True:
                wait = self._next_ok - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    resp = await super().send(request, **kwargs)
                finally:
                    self.requests_made += 1
                    self._next_ok = time.monotonic() + self.next_gap()
                if resp.status_code not in THROTTLE_STATUSES:
                    self._consecutive_throttles = 0
                    return resp

                self.throttle_events += 1
                self._consecutive_throttles += 1
                await resp.aclose()
                if self._consecutive_throttles >= self.max_throttles:
                    raise SourceThrottled(
                        f"{request.url.host} returned {resp.status_code} {self._consecutive_throttles}x in a row; "
                        "stopping this source for this run"
                    )
                backoff = _retry_after_seconds(resp) or BACKOFF_START_SECONDS * (2 ** attempt)
                backoff = min(backoff, BACKOFF_MAX_SECONDS)
                log.warning("%s throttled (%d) on %s; backing off %.0fs",
                            request.url.host, resp.status_code, request.url.path, backoff)
                await asyncio.sleep(backoff)
                attempt += 1
