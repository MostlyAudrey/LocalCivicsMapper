"""URL discovery: RSS/Atom feeds and (WordPress / Yoast / generic) sitemaps, with date-window
pre-filtering on `<lastmod>` and `/YYYY/MM/` URL paths so backfills don't fetch years of pages."""
from __future__ import annotations

import asyncio
import gzip
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

SITEMAP_ROOT_CANDIDATES = ("/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml")

# Child sitemaps that never contain articles (Yoast + core WordPress naming).
_NON_ARTICLE_SITEMAP = re.compile(
    r"(category|post_tag|tag-sitemap|author|users|taxonomies|page-sitemap|wp-sitemap-posts-page|"
    r"attachment|media|product|web-story|elementor|tribe_|event|venue|organizer|classifieds|image)",
    re.IGNORECASE,
)
_URL_DATE = re.compile(r"/((?:19|20)\d{2})/(0?[1-9]|1[0-2])(?:/(0?[1-9]|[12]\d|3[01]))?(?:/|$)")
_MAX_SITEMAPS = 2000


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: Optional[datetime]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_lastmod(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        try:
            dt = datetime.fromisoformat(v[:10])
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_sitemap(content: bytes) -> Tuple[str, List[SitemapEntry]]:
    """Return ('index'|'urlset', entries). Raises ET.ParseError on non-XML."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = ET.fromstring(content)
    kind = "index" if _local(root.tag) == "sitemapindex" else "urlset"
    entries: List[SitemapEntry] = []
    for node in root:
        if _local(node.tag) not in ("sitemap", "url"):
            continue
        loc = lastmod = None
        for child in node:
            name = _local(child.tag)
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod":
                lastmod = parse_lastmod(child.text)
        if loc:
            entries.append(SitemapEntry(loc, lastmod))
    return kind, entries


def url_path_date(url: str) -> Optional[date]:
    """Date encoded in a /YYYY/MM/(DD/) path, using day 1 when only the month is present."""
    m = _URL_DATE.search(url)
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))


def url_may_be_in_window(entry: SitemapEntry, since: Optional[date], until: Optional[date],
                         strict_until: bool = False) -> bool:
    """Cheap pre-filter. Conservative: only rejects when evidence says the URL is out of window.

    strict_until (history walk): also treat lastmod > until as "too new". An old article edited after
    `until` is skipped - a small loss that stops every pass re-downloading the same newer pages."""
    if since is not None and entry.lastmod is not None and entry.lastmod.date() < since:
        return False  # last modified before the window => published before it too
    if strict_until and until is not None and entry.lastmod is not None and entry.lastmod.date() > until:
        return False
    pd = url_path_date(entry.loc)
    if pd is not None:
        has_day = bool(_URL_DATE.search(entry.loc).group(3))  # type: ignore[union-attr]
        if since is not None:
            if has_day and pd < since:
                return False
            if not has_day and (pd.year, pd.month) < (since.year, since.month):
                return False
        if until is not None and pd > until:
            return False
    return True


def newest_first(entries: List[SitemapEntry]) -> List[SitemapEntry]:
    """Sort by lastmod, else /YYYY/MM/DD/ path date, newest first (undated last), so that
    `--limit N` takes the most recent N articles regardless of sitemap order."""
    def key(e: SitemapEntry):
        if e.lastmod is not None:
            return (1, e.lastmod.date())
        pd = url_path_date(e.loc)
        return (1, pd) if pd else (0, date.min)
    return sorted(entries, key=key, reverse=True)


def child_sitemap_may_be_in_window(entry: SitemapEntry, since: Optional[date]) -> bool:
    if _NON_ARTICLE_SITEMAP.search(entry.loc):
        return False
    if since is not None and entry.lastmod is not None and entry.lastmod.date() < since:
        return False
    return True


async def _get(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    try:
        r = await client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.warning("sitemap fetch failed %s: %s", url, exc)
        return None
    if r.status_code != 200:
        return None
    return r.content


async def discover_sitemap_urls(
    client: httpx.AsyncClient,
    base_url: str,
    sitemap_url: Optional[str],
    since: Optional[date],
    until: Optional[date] = None,
    delay: float = 0.0,
    strict_until: bool = False,
) -> List[str]:
    """Walk sitemap indexes breadth-first and return in-window article URLs, newest first."""
    roots = [sitemap_url] if sitemap_url else []
    roots += [base_url.rstrip("/") + p for p in SITEMAP_ROOT_CANDIDATES if base_url.rstrip("/") + p != sitemap_url]

    queue: List[str] = []
    for root in roots:
        content = await _get(client, root)
        if content:
            try:
                parse_sitemap(content)
            except ET.ParseError:
                continue
            queue.append(root)
            break
    if not queue:
        log.warning("no sitemap found for %s", base_url)
        return []

    visited: set[str] = set()
    urls: List[SitemapEntry] = []
    seen_urls: set[str] = set()
    while queue and len(visited) < _MAX_SITEMAPS:
        sm = queue.pop(0)
        if sm in visited:
            continue
        visited.add(sm)
        content = await _get(client, sm)
        if delay:
            await asyncio.sleep(delay)
        if not content:
            continue
        try:
            kind, entries = parse_sitemap(content)
        except ET.ParseError:
            log.warning("unparseable sitemap %s", sm)
            continue
        if kind == "index":
            queue.extend(e.loc for e in entries if child_sitemap_may_be_in_window(e, since))
        else:
            for e in entries:
                if e.loc not in seen_urls and url_may_be_in_window(e, since, until, strict_until):
                    seen_urls.add(e.loc)
                    urls.append(e)
    return [e.loc for e in newest_first(urls)]


@dataclass
class WpPost:
    url: str
    published_at: Optional[datetime]
    title: str
    content_html: str
    author: Optional[str]


WP_POST_FIELDS = "id,link,date_gmt,title,content,_links,_embedded"


def wp_posts_endpoint(base_url: str, api_base: Optional[str] = None) -> str:
    """Self-hosted: {base}/wp-json/wp/v2/posts. WordPress.com-hosted sites set scrape_config.wp_api_base
    to https://public-api.wordpress.com/wp/v2/sites/<domain>."""
    return (api_base.rstrip("/") if api_base else base_url.rstrip("/") + "/wp-json/wp/v2") + "/posts"


async def wp_api_available(client: httpx.AsyncClient, base_url: str, api_base: Optional[str] = None) -> bool:
    try:
        r = await client.get(wp_posts_endpoint(base_url, api_base), params={"per_page": 1, "_fields": "id"})
    except httpx.HTTPError:
        return False
    if r.status_code != 200:
        return False
    try:
        return isinstance(r.json(), list)
    except ValueError:
        return False


async def discover_wp_api_posts(
    client: httpx.AsyncClient,
    base_url: str,
    since: Optional[date],
    until: Optional[date] = None,
    max_posts: Optional[int] = None,
    delay: float = 0.0,
    skip_urls: Optional[set] = None,
    api_base: Optional[str] = None,
    extra_query: Optional[dict] = None,
) -> List[WpPost]:
    """Page through /wp-json/wp/v2/posts newest-first within [since, until].

    `skip_urls` (already stored) don't count toward `max_posts`, so a limited incremental crawl
    keeps paging until it has `max_posts` genuinely new posts.
    """
    import html as htmllib

    params: dict = {"per_page": 50, "orderby": "date", "order": "desc", "_fields": WP_POST_FIELDS, "_embed": "author"}
    # scrape_config.wp_query: extra WP REST filters, e.g. {"categories": 12} to fetch only one section.
    # Core keys above (paging, fields, dates) can't be overridden.
    for k, v in (extra_query or {}).items():
        if k not in params and k not in ("page", "after", "before"):
            params[k] = v
    if since:
        params["after"] = f"{since.isoformat()}T00:00:00"
    if until:
        params["before"] = f"{until.isoformat()}T23:59:59"
    endpoint = wp_posts_endpoint(base_url, api_base)
    skip_urls = skip_urls or set()
    posts: List[WpPost] = []
    page = 1
    while True:
        try:
            r = await client.get(endpoint, params={**params, "page": page})
        except httpx.HTTPError as exc:
            log.warning("wp-json page %d failed: %s", page, exc)
            break
        if r.status_code == 400:  # past the last page
            break
        if r.status_code != 200:
            log.warning("wp-json page %d returned %d", page, r.status_code)
            break
        batch = r.json()
        if not batch:
            break
        for p in batch:
            link = p.get("link")
            if not link or link in skip_urls:
                continue
            author = None
            for a in (p.get("_embedded") or {}).get("author") or []:
                if isinstance(a, dict) and a.get("name"):
                    author = a["name"]
                    break
            posts.append(WpPost(
                url=link,
                published_at=parse_lastmod(p.get("date_gmt") + "Z") if p.get("date_gmt") else None,
                title=htmllib.unescape((p.get("title") or {}).get("rendered") or "").strip(),
                content_html=(p.get("content") or {}).get("rendered") or "",
                author=author,
            ))
            if max_posts and len(posts) >= max_posts:
                return posts
        total_pages = int(r.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
        if delay:
            await asyncio.sleep(delay)
    return posts


async def discover_rss_urls(
    client: httpx.AsyncClient,
    feed_url: str,
    since: Optional[date],
    until: Optional[date] = None,
) -> List[str]:
    """Article links from one RSS 2.0 / Atom feed fetched through the (polite) client, filtered by
    pubDate/updated when present. One request instead of a sitemap walk."""
    from email.utils import parsedate_to_datetime

    content = await _get(client, feed_url)
    if not content:
        return []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        log.warning("unparseable feed %s", feed_url)
        return []
    entries: List[SitemapEntry] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        link = when = None
        for child in node:
            name = _local(child.tag)
            if name == "link":
                link = (child.text or child.get("href") or "").strip() or link
            elif name in ("pubDate", "published", "updated") and child.text and when is None:
                try:
                    when = parsedate_to_datetime(child.text.strip())
                except (TypeError, ValueError):
                    when = parse_lastmod(child.text)
        if not link:
            continue
        if when is not None:
            d = when.date()
            if (since and d < since) or (until and d > until):
                continue
        entries.append(SitemapEntry(link, when))
    return [e.loc for e in newest_first(entries)]


_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)


async def discover_listing_links(
    client: httpx.AsyncClient,
    listing_urls: List[str],
    article_pattern: str,
    since: Optional[date],
    until: Optional[date] = None,
    delay: float = 0.0,
) -> List[str]:
    """Collect article links from listing pages (homepage / section fronts) for sites with no
    feed, sitemap or API. `article_pattern` is a regex matched against the absolute URL."""
    from urllib.parse import urljoin

    pat = re.compile(article_pattern)
    out: List[str] = []
    seen: set[str] = set()
    for i, page in enumerate(listing_urls):
        if i and delay:
            await asyncio.sleep(delay)
        content = await _get(client, page)
        if not content:
            continue
        for href in _HREF.findall(content.decode("utf-8", "replace")):
            url = urljoin(page, href.strip())
            if url in seen or not pat.search(url):
                continue
            seen.add(url)
            if url_may_be_in_window(SitemapEntry(url, None), since, until):
                out.append(url)
    return [e.loc for e in newest_first([SitemapEntry(u, None) for u in out])]


async def discover_feed_urls(feed_or_site_url: str) -> List[str]:
    """Article URLs from an RSS/Atom feed (or feeds auto-discovered on the homepage)."""
    from trafilatura import feeds

    try:
        return list(await asyncio.to_thread(feeds.find_feed_urls, feed_or_site_url))
    except Exception as exc:  # noqa: BLE001 - feed problems must not abort the crawl
        log.warning("feed discovery failed for %s: %s", feed_or_site_url, exc)
        return []
