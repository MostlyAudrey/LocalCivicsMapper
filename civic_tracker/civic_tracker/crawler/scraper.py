"""Polite downloader + trafilatura extraction + window verification + TB_Article insert."""
from __future__ import annotations

import asyncio
import hashlib
import re
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib import robotparser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import trafilatura
from psycopg_pool import AsyncConnectionPool
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from civic_tracker.config import settings
from civic_tracker.crawler.discovery import (
    discover_feed_urls,
    discover_sitemap_urls,
    discover_listing_links,
    discover_rss_urls,
    discover_wp_api_posts,
    wp_api_available,
)
from civic_tracker.crawler import agendacenter, civicclerk, pdflinks
from civic_tracker.crawler.polite import PoliteClient, SourceThrottled
from civic_tracker.db import repositories as repo
from civic_tracker.pipeline.triage import url_allowed

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 300
RECENT_DEFAULT_DAYS = 3
# Safety valve for incremental crawls: page-by-page sources (html/feed/links) download at most this many
# new URLs per crawl. Undated sitemap entries (tag/category pages) otherwise all count as "recent".
RECENT_MAX_NEW_URLS = 100
RECORDS_RECHECK_DAYS = 60  # civicclerk: supplemental agenda items, and minutes posted weeks after the meeting
AGENDACENTER_RECHECK_DAYS = 60  # agendacenter: minutes appear weeks after the meeting


def _host(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else (host or None)


def source_hosts(source: Dict[str, Any]) -> set:
    """Every host a crawl of this source may contact."""
    cfg = source.get("scrape_config") or {}
    urls = [source.get("base_url"), source.get("rss_url"), source.get("sitemap_url"), cfg.get("wp_api_base"),
            *(cfg.get("listing_urls") or [])]
    hosts = {h for h in map(_host, urls) if h}
    if cfg.get("method") == "civicclerk" and cfg.get("tenant"):
        hosts.update(civicclerk.tenant_hosts(cfg["tenant"]))  # all tenants share the PDF blob host
    if cfg.get("crawl_group"):  # e.g. papers on one publishing platform that rate-limits them together
        hosts.add(f"group:{cfg['crawl_group']}")
    return hosts


def host_groups(sources: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Partition sources so that any two sharing a host land in the same group (order kept).
    Groups can be crawled concurrently; sources inside a group must run sequentially."""
    groups: List[List[Dict[str, Any]]] = []
    group_hosts: List[set] = []
    for s in sources:
        hosts = source_hosts(s)
        hits = [i for i, gh in enumerate(group_hosts) if gh & hosts]
        if not hits:
            groups.append([s])
            group_hosts.append(set(hosts))
            continue
        first = hits[0]
        for i in reversed(hits[1:]):  # this source bridges several groups: merge them
            groups[first] += groups.pop(i)
            group_hosts[first] |= group_hosts.pop(i)
        groups[first].append(s)
        group_hosts[first] |= hosts
    return groups


async def for_each_source(
    sources: List[Dict[str, Any]],
    fn: Callable[[Dict[str, Any]], Any],
    parallel: int = settings.CRAWL_PARALLEL_SOURCES,
) -> None:
    """Run `fn(source)` for every source: different sites concurrently (at most `parallel` at once),
    sources sharing a host strictly one after another. `fn` should handle its own errors."""
    sem = asyncio.Semaphore(max(1, parallel))

    async def run_group(group: List[Dict[str, Any]]) -> None:
        for s in group:
            async with sem:
                await fn(s)

    await asyncio.gather(*(run_group(g) for g in host_groups(sources)))


@dataclass
class CrawlStats:
    discovered: int = 0
    new_urls: int = 0
    inserted: int = 0
    out_of_window: int = 0
    no_date: int = 0
    duplicate_content: int = 0
    too_short: int = 0
    disallowed: int = 0
    errors: int = 0
    method: str = ""
    requests: int = 0
    throttled: int = 0
    filtered: int = 0  # dropped by section filters (url_include / url_exclude) before download
    error_samples: List[str] = field(default_factory=list)


def make_client(delay: float = settings.CRAWL_DELAY_SECONDS) -> PoliteClient:
    return PoliteClient(delay=delay, max_throttles=settings.CRAWL_MAX_THROTTLES)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (500, 502, 504)


@retry(retry=retry_if_exception(_retryable), stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30), reraise=True)
async def fetch_html(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url)
    r.raise_for_status()
    return r.text


def extract_article(html: str, url: str) -> Optional[Dict[str, Any]]:
    """trafilatura bare_extraction -> plain dict (handles both 1.x dicts and 2.x Document objects).

    Blockquotes are retained by trafilatura's default text extraction; there is no separate
    quotes flag in the bare_extraction API.
    """
    doc = trafilatura.bare_extraction(
        html,
        url=url,
        include_links=True,
        include_comments=False,
        with_metadata=True,
    )
    if doc is None:
        return None
    if isinstance(doc, dict):
        return doc
    if hasattr(doc, "as_dict"):
        return doc.as_dict()
    return {k: getattr(doc, k, None) for k in ("title", "author", "date", "text", "url")}


def parse_published_at(value: Optional[str], tz_name: str = "UTC") -> Optional[datetime]:
    """htmldate yields 'YYYY-MM-DD' (sometimes with time). Return a tz-aware datetime."""
    if not value:
        return None
    tz = ZoneInfo(tz_name)
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.combine(date.fromisoformat(value.strip()[:10]), time(0, 0))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Robots:
    def __init__(self) -> None:
        self._cache: Dict[str, Optional[robotparser.RobotFileParser]] = {}

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            rp: Optional[robotparser.RobotFileParser] = robotparser.RobotFileParser()
            try:
                r = await client.get(origin + "/robots.txt")
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())  # type: ignore[union-attr]
                else:
                    rp = None  # no robots.txt => allow all
            except httpx.HTTPError:
                rp = None
            self._cache[origin] = rp
        rp = self._cache[origin]
        return True if rp is None else rp.can_fetch(settings.USER_AGENT, url)

    def crawl_delay(self, url: str) -> Optional[float]:
        parts = urlsplit(url)
        rp = self._cache.get(f"{parts.scheme}://{parts.netloc}")
        if rp is None:
            return None
        d = rp.crawl_delay(settings.USER_AGENT) or rp.crawl_delay("*")
        return float(d) if d else None


def _strip_boilerplate(text: str, markers: List[str]) -> str:
    """Cut the text at the first occurrence of any configured footer marker."""
    for m in markers:
        i = text.find(m)
        if i != -1:
            text = text[:i]
    return text.rstrip()


async def _store(
    pool: AsyncConnectionPool,
    stats: CrawlStats,
    source: Dict[str, Any],
    *,
    url: str,
    title: Optional[str],
    author: Optional[str],
    published_at: Optional[datetime],
    text: str,
    since: Optional[date],
    until: Optional[date],
    backfill: bool,
    genre: Optional[str] = None,
    doc_meta: Optional[dict] = None,
) -> None:
    """Shared verify + dedup + insert path for HTML, WP-API articles and official records."""
    cfg = source.get("scrape_config") or {}
    text = _strip_boilerplate(text, cfg.get("strip_after", []))
    if title and cfg.get("title_suffix_re"):
        title = re.sub(cfg["title_suffix_re"], "", title).strip()
    if len(text) < MIN_TEXT_CHARS:
        stats.too_short += 1
        return
    if published_at is None:
        if backfill:
            stats.no_date += 1
            return
    else:
        pd = published_at.date()
        if (since and pd < since) or (until and pd > until):
            stats.out_of_window += 1
            return
    content_hash = sha256_text(text)
    async with pool.connection() as conn:
        if await repo.hash_exists(conn, content_hash):
            stats.duplicate_content += 1
            return
        new_id = await repo.insert_article(
            conn, source_id=source["id"], url=url, title=(title or url).strip(), author=author,
            published_at=published_at, full_text=text, content_hash=content_hash,
            genre=genre, doc_meta=doc_meta,
        )
    if new_id:
        stats.inserted += 1


async def _crawl_civicclerk(
    pool: AsyncConnectionPool,
    client: PoliteClient,
    robots: "_Robots",
    source: Dict[str, Any],
    stats: CrawlStats,
    *,
    since: Optional[date],
    until: Optional[date],
    backfill: bool,
    limit: Optional[int],
    report: Callable[[str], None],
) -> None:
    """Official records from a CivicClerk tenant. `limit` caps new records, but the meeting in progress is always
    finished (the history cursor is the oldest stored date, so a half-stored meeting is revisited anyway)."""
    cfg = source.get("scrape_config") or {}
    api = civicclerk.api_base(cfg["tenant"])
    if not await robots.allowed(client, api + "/Events"):
        stats.disallowed += 1
        return
    client.raise_delay(robots.crawl_delay(api))

    async def is_new(urls: List[str]) -> List[str]:
        async with pool.connection() as conn:
            return await repo.filter_new_urls(conn, urls)

    async def known_prefix(prefix: str) -> bool:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1 FROM TB_Article WHERE url LIKE %s LIMIT 1",
                                     (prefix.replace("%", r"\%").replace("_", r"\_") + "%",))
            return await cur.fetchone() is not None

    async def allowed(url: str) -> bool:
        return await robots.allowed(client, url)

    current_meeting = None
    async for doc in civicclerk.discover_records(client, cfg, since=since, until=until, is_new=is_new,
                                                 known_prefix=known_prefix, allowed=allowed, report=report,
                                                 stats=stats):
        if isinstance(doc, civicclerk.RecordError):
            stats.errors += 1
            if len(stats.error_samples) < 5:
                stats.error_samples.append(str(doc)[:400])
            log.warning("record failed: %s", doc)
            continue
        meeting = doc.doc_meta.get("event_id")
        if limit and stats.inserted >= limit and meeting != current_meeting:
            break
        current_meeting = meeting
        stats.new_urls += 1
        await _store(pool, stats, source, url=doc.url, title=doc.title, author=None, published_at=doc.published_at,
                     text=doc.text, since=since, until=until, backfill=backfill,
                     genre="official_record", doc_meta=doc.doc_meta)


async def _crawl_agendacenter(
    pool: AsyncConnectionPool,
    client: PoliteClient,
    robots: "_Robots",
    source: Dict[str, Any],
    stats: CrawlStats,
    *,
    since: Optional[date],
    until: Optional[date],
    backfill: bool,
    limit: Optional[int],
    report: Callable[[str], None],
) -> None:
    """Agendas + minutes from a CivicPlus Agenda Center (crawler/agendacenter.py). `limit` caps new records."""
    cfg = source.get("scrape_config") or {}

    async def is_new(urls: List[str]) -> List[str]:
        async with pool.connection() as conn:
            return await repo.filter_new_urls(conn, urls)

    async def allowed(url: str) -> bool:
        return await robots.allowed(client, url)

    if cfg.get("method") == "pdf_links":
        gen = pdflinks.discover_records(client, cfg, since=since, until=until, is_new=is_new, allowed=allowed,
                                        report=report, stats=stats)
    else:
        gen = agendacenter.discover_records(client, source["base_url"], cfg, since=since, until=until,
                                            is_new=is_new, allowed=allowed, report=report, stats=stats)
    async for doc in gen:
        if isinstance(doc, civicclerk.RecordError):
            stats.errors += 1
            if len(stats.error_samples) < 5:
                stats.error_samples.append(str(doc)[:400])
            log.warning("record failed: %s", doc)
            continue
        if limit and stats.inserted >= limit:
            break
        stats.new_urls += 1
        await _store(pool, stats, source, url=doc.url, title=doc.title, author=None, published_at=doc.published_at,
                     text=doc.text, since=since, until=until, backfill=backfill,
                     genre="official_record", doc_meta=doc.doc_meta)


async def crawl_source(
    pool: AsyncConnectionPool,
    source: Dict[str, Any],
    *,
    since: Optional[date] = None,
    until: Optional[date] = None,
    backfill: bool = False,
    limit: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
    history: bool = False,
) -> CrawlStats:
    """Discover, download, verify and insert articles for one source.

    Discovery method comes from scrape_config.method:
      "wp_api"  - WordPress REST API (/wp-json/wp/v2/posts): exact dates + body, no page fetches.
      "html"    - RSS (recent mode) + sitemaps, then per-page download + trafilatura.
      "feed"    - one RSS/Atom request to rss_url (fetched politely), then per-page download.
                  Lightest option; preferred for rate-limited sites.
      "links"   - article links scraped from scrape_config.listing_urls (default: homepage) that
                  match scrape_config.article_pattern; then per-page download like "html".
      "auto"    - (default) wp_api if the endpoint answers, else html.
      "civicclerk" - city council agendas (crawler/civicclerk.py): one 'official_record' per staff-reported
                  agenda item. scrape_config: tenant, jurisdiction, bodies (CivicClerk categoryName list).
      "agendacenter" - CivicPlus Agenda Center on base_url (crawler/agendacenter.py): one 'official_record'
                  per agenda/packet and per minutes PDF. scrape_config: jurisdiction, categories (names or ids,
                  default all), exclude_categories, include_minutes (default true).
    backfill=True: articles without a parseable date are skipped.
    backfill=False (recent): window starts at last crawl - 1 day (or the last few days).
    Other scrape_config keys: timezone (for date-only values), strip_after (footer markers),
    title_suffix_re (regex removed from the end of titles, e.g. " | The Aggie$"),
    wp_api_base (WordPress.com-hosted sites), crawl_delay (seconds, overrides the global setting).
    """
    stats = CrawlStats()
    report = progress or (lambda _msg: None)
    cfg = source.get("scrape_config") or {}
    tz_name = cfg.get("timezone", "UTC")
    method = cfg.get("method", "auto")
    api_base = cfg.get("wp_api_base")
    delay = max(settings.CRAWL_DELAY_SECONDS, float(cfg.get("crawl_delay", 0)))
    robots = _Robots()

    if not backfill and since is None:
        last = source.get("last_crawled_at")
        since = (last.date() - timedelta(days=1)) if last else (date.today() - timedelta(days=RECENT_DEFAULT_DAYS))
        if method == "civicclerk":
            # agendas are posted days ahead and supplemented later: re-check the last few weeks (known items
            # cost no PDF download) and include upcoming meetings (no upper bound)
            since = min(since, date.today() - timedelta(days=RECORDS_RECHECK_DAYS))
        elif method in ("agendacenter", "pdf_links"):
            # minutes are posted weeks after the meeting: look further back (known files cost nothing)
            since = min(since, date.today() - timedelta(days=AGENDACENTER_RECHECK_DAYS))

    throttled = False
    async with make_client(delay) as client:
      try:
        # robots.txt first: honours Crawl-delay for every later request (sitemaps, API, pages)
        await robots.allowed(client, source["base_url"])
        client.raise_delay(robots.crawl_delay(source["base_url"]))
        report("discovering articles")
        if method == "auto":
            method = "wp_api" if await wp_api_available(client, source["base_url"], api_base) else "html"
        stats.method = method

        if method == "civicclerk":
            await _crawl_civicclerk(pool, client, robots, source, stats, since=since, until=until,
                                    backfill=backfill, limit=limit, report=report)
        elif method in ("agendacenter", "pdf_links"):  # both yield official-record PDFs
            await _crawl_agendacenter(pool, client, robots, source, stats, since=since, until=until,
                                      backfill=backfill, limit=limit, report=report)
        elif method == "wp_api":
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT url FROM TB_Article WHERE source_id = %s", (source["id"],))
                known = {r["url"] for r in await cur.fetchall()}
            posts = await discover_wp_api_posts(
                client, source["base_url"], since, until, max_posts=limit,
                skip_urls=known, api_base=api_base, extra_query=cfg.get("wp_query"),
            )
            stats.discovered = len(posts)
            posts = [p for p in posts if url_allowed(p.url, cfg)]  # section filters (no fetch needed here,
            stats.filtered = stats.discovered - len(posts)          # but don't store excluded sections)
            stats.new_urls = len(posts)
            for i, post in enumerate(posts, 1):
                report(f"storing {i}/{len(posts)}: {post.title[:60]}")
                try:
                    wrapped = f"<html><head><title>{post.title}</title></head><body><article>{post.content_html}</article></body></html>"
                    data = await asyncio.to_thread(extract_article, wrapped, post.url) or {}
                    await _store(pool, stats, source, url=post.url, title=post.title, author=post.author,
                                 published_at=post.published_at, text=data.get("text") or "",
                                 since=since, until=until, backfill=backfill)
                except SourceThrottled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    stats.errors += 1
                    if len(stats.error_samples) < 5:
                        stats.error_samples.append(f"{post.url}: {type(exc).__name__}: {exc}")
        else:
            urls: List[str] = []
            if method == "feed":
                urls += await discover_rss_urls(client, source["rss_url"], since, until)
            elif method == "links":
                urls += await discover_listing_links(
                    client, cfg.get("listing_urls") or [source["base_url"]], cfg["article_pattern"],
                    since, until,
                )
            else:
                if not backfill:
                    urls += await discover_feed_urls(source.get("rss_url") or source["base_url"])
                urls += await discover_sitemap_urls(
                    client, source["base_url"], source.get("sitemap_url"), since, until, strict_until=history
                )
            stats.discovered = len(urls)
            allowed = [u for u in urls if url_allowed(u, cfg)]  # section filters BEFORE any page download
            stats.filtered = len(urls) - len(allowed)
            async with pool.connection() as conn:
                new_urls = await repo.filter_new_urls(conn, allowed)
            cap = limit or (None if backfill else RECENT_MAX_NEW_URLS)
            if cap and len(new_urls) > cap:
                if not limit:
                    log.warning("%s: %d 'recent' URLs to fetch - capped at %d; check the source's url filters",
                                source["name"], len(new_urls), cap)
                    stats.error_samples.append(f"{len(new_urls)} recent URLs found, only the first {cap} fetched "
                                               "(add url_include/url_exclude if these aren't articles)")
                new_urls = new_urls[:cap]
            stats.new_urls = len(new_urls)

            for i, url in enumerate(new_urls, 1):  # spacing between requests is enforced by PoliteClient
                report(f"fetching {i}/{len(new_urls)} (every {client.delay:.0f}s): {url[-70:]}")
                try:
                    if not await robots.allowed(client, url):
                        stats.disallowed += 1
                        continue
                    html = await fetch_html(client, url)
                    data = await asyncio.to_thread(extract_article, html, url) or {}
                    await _store(pool, stats, source, url=url, title=data.get("title"), author=data.get("author"),
                                 published_at=parse_published_at(data.get("date"), tz_name),
                                 text=data.get("text") or "", since=since, until=until, backfill=backfill)
                except SourceThrottled:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad page must not stop the crawl
                    stats.errors += 1
                    if len(stats.error_samples) < 5:
                        stats.error_samples.append(f"{url}: {type(exc).__name__}: {exc}")
                    log.warning("failed %s: %s", url, exc)
      except SourceThrottled as exc:
        throttled = True
        stats.errors += 1
        stats.error_samples.append(str(exc))
        log.warning("%s", exc)
      finally:
        stats.requests = client.requests_made
        stats.throttled = client.throttle_events

    if not throttled:  # leave last_crawled_at alone so the next incremental run retries the window
        async with pool.connection() as conn:
            await repo.touch_source_crawled(conn, source["id"])
    return stats


HISTORY_METHODS = {"wp_api", "html", "auto", "civicclerk", "agendacenter", "pdf_links"}  # can page back in time themselves


def history_method(cfg: Dict[str, Any]) -> Optional[str]:
    """Method used to walk a source back in time. Feed/links sources only expose recent articles, so they
    fall back to the site's sitemaps ("html"). scrape_config.history_method overrides; "none" disables."""
    override = cfg.get("history_method")
    if override:
        return None if override == "none" else override
    method = cfg.get("method", "auto")
    return method if method in HISTORY_METHODS else "html"


async def crawl_source_history(
    pool: AsyncConnectionPool,
    source: Dict[str, Any],
    *,
    floor: date,
    batch: int,
    progress: Optional[Callable[[str], None]] = None,
) -> Optional[CrawlStats]:
    """History walk: queue the next `batch` articles published before this source's oldest stored article,
    down to `floor`. Returns None when the source can't (or no longer needs to) walk back.

    State lives in scrape_config: history_done / history_done_at / history_stalls. A source is marked done
    when discovery finds nothing older, or after 3 passes in a row that queue nothing (stuck pages)."""
    from psycopg.types.json import Jsonb

    cfg = dict(source.get("scrape_config") or {})
    hmethod = history_method(cfg)
    if cfg.get("history_done") or not hmethod:
        return None
    floor = date.fromisoformat(cfg["history_floor"]) if cfg.get("history_floor") else floor
    batch = int(cfg.get("history_batch", batch))
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT MIN(published_at) AS oldest FROM TB_Article WHERE source_id = %s",
                                 (source["id"],))
        oldest = (await cur.fetchone())["oldest"]
    # nothing stored yet (nothing recent either): start walking back from today
    until = oldest.date() if oldest is not None else date.today() + timedelta(days=1)
    if until <= floor:
        updates = {"history_done": True, "history_done_at": date.today().isoformat()}
        stats = None
    else:
        walk_source = source
        if hmethod == "feed_dated":
            # search-style feeds (e.g. TownNews BLOX /search/?f=rss) accept a date range: ask for the newest
            # items published before the cursor. If a site ignores d1/d2, every item is newer than `until`,
            # discovery finds nothing in the window and the walk ends - it never re-fetches recent pages.
            sep = "&" if "?" in (source.get("rss_url") or "") else "?"
            walk_source = {**source, "rss_url": f"{source['rss_url']}{sep}d1={floor.isoformat()}&d2={until.isoformat()}",
                           "scrape_config": {**cfg, "method": "feed"}}
        elif hmethod != cfg.get("method", "auto"):
            walk_source = {**source, "scrape_config": {**cfg, "method": hmethod}}
        stats = await crawl_source(pool, walk_source, since=floor, until=until, backfill=True, limit=batch,
                                   progress=progress, history=True)
        updates = {}
        if stats.discovered == 0 and stats.errors == 0:
            updates = {"history_done": True, "history_done_at": date.today().isoformat(), "history_stalls": 0}
        elif stats.inserted == 0:
            stalls = int(cfg.get("history_stalls", 0)) + 1
            updates = {"history_stalls": stalls}
            if stalls >= 3:
                updates.update(history_done=True, history_done_at=date.today().isoformat())
        elif cfg.get("history_stalls"):
            updates = {"history_stalls": 0}
    if updates:
        async with pool.connection() as conn:
            await conn.execute("UPDATE TB_Sources SET scrape_config = scrape_config || %s WHERE id = %s",
                               (Jsonb(updates), source["id"]))
    return stats


def window_from_args(backfill_days: Optional[int], since: Optional[str]) -> Optional[date]:
    if since:
        return date.fromisoformat(since)
    if backfill_days:
        return datetime.now(timezone.utc).date() - timedelta(days=backfill_days)
    return None
