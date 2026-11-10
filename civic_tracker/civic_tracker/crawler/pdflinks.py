"""Generic "PDF links" crawler for agencies that post meeting documents as PDF links on ordinary web pages
(Yolo LAFCo, Yolo County Office of Education, Valley Clean Energy, First 5 Yolo, flood control / water / housing /
air districts...). One small scrape_config per agency instead of one scraper each:

  listing_urls     pages that link to the PDFs. "{year}" expands to each year in the crawl window, newest first
                   (sites with one page per year, e.g. ".../Board/{year}-Board-Meetings/index.html").
  follow_pattern   optional regex: also open links on the listing pages that match it, one level deep
                   (sites with one page per meeting), and collect PDFs there.
  pdf_pattern      regex a document link must match (default: ends in .pdf).
  include          regex on "link text + file name" a document must match (default: agenda/minutes/packet/
                   staff report/resolution/ordinance/action ...); `exclude` drops matches (flyers, forms...).
  body, jurisdiction   used in record titles ("Yolo LAFCo, 2026-10-23: Agenda Packet").
  date_order       "mdy" (default, US) for numeric dates like 10.23.26.
  date_from_context  true = when neither link text nor file name has a date, use the text just before the link
                   (off by default: on list pages that text can belong to the previous row).

The meeting date comes from the link text, else the file name, else the listing context (the text just before
the link). Documents without a readable date are skipped (counted as no_date): they can't be placed in time.
Every request goes through the caller's PoliteClient and robots.txt checks.
"""
from __future__ import annotations

import html as _html
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from civic_tracker.crawler.civicclerk import MAX_TEXT_CHARS, RecordDoc, RecordError, _pages, join_pages
from civic_tracker.crawler.polite import SourceThrottled

log = logging.getLogger(__name__)

DEFAULT_INCLUDE = (r"agenda|minutes|packet|staff\s*report|resolution|ordinance|action|summary|report|"
                   r"recommendation|budget|memo")
DEFAULT_EXCLUDE = r"flyer|brochure|application|form\b|map\b|calendar|newsletter|presentation\s+slides"
_A = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_D_ISO = re.compile(r"(20\d{2}|19\d{2})[-_.](\d{1,2})[-_.](\d{1,2})")
_D_NUM = re.compile(r"(?<!\d)(\d{1,2})[-_./](\d{1,2})[-_./](\d{4}|\d{2})(?!\d)")
_D_COMPACT = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")  # 10282026
_D_COMPACT_YMD = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")  # 20261103
_D_TEXT = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?"
                     r"\s+(\d{4})", re.I)


def _mk(y: int, m: int, d: int) -> Optional[date]:
    if y < 100:
        y += 2000
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    return dt if 1990 <= dt.year <= date.today().year + 1 else None


def parse_doc_date(text: str, order: str = "mdy") -> Optional[date]:
    """First plausible date in `text`: 'October 28, 2026', '2026-10-28', '10.28.26', '10282026'."""
    t = unquote(text or "").replace("+", " ")
    m = _D_TEXT.search(t)
    if m:
        d = _mk(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        if d:
            return d
    m = _D_ISO.search(t)
    if m and (d := _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))):
        return d
    for m in _D_NUM.finditer(t):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = _mk(y, b, a) if order == "dmy" else _mk(y, a, b)
        if d:
            return d
    m = _D_COMPACT_YMD.search(t)
    if m and (d := _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))):
        return d
    m = _D_COMPACT.search(t)
    if m and (d := _mk(int(m.group(3)), int(m.group(1)), int(m.group(2)))):
        return d
    return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAGS.sub(" ", s or ""))).strip()


def find_links(page: str, page_url: str) -> List[Dict[str, Any]]:
    """All links on a page: absolute url, link text, and ~200 chars of text before the link (context)."""
    out = []
    for m in _A.finditer(page):
        href = _html.unescape(m.group(1)).strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        ctx = _clean(page[max(0, m.start() - 600):m.start()])[-200:]
        out.append({"url": urljoin(page_url, href), "text": _clean(m.group(2)), "context": ctx})
    return out


def document_kind(text: str, url: str) -> str:
    s = f"{text} {unquote(urlsplit(url).path)}".lower()
    for word, label in (("minutes", "Minutes"), ("packet", "Agenda Packet"), ("staff report", "Staff Report"),
                        ("resolution", "Resolution"), ("ordinance", "Ordinance"), ("agenda", "Agenda"),
                        ("budget", "Budget"), ("report", "Report")):
        if word in s:
            return label
    return "Document"


def select_documents(links: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    pdf_rx = re.compile(cfg.get("pdf_pattern") or r"\.pdf(?:$|[?#])", re.I)
    inc = re.compile(cfg.get("include") or DEFAULT_INCLUDE, re.I)
    exc = re.compile(cfg.get("exclude") or DEFAULT_EXCLUDE, re.I)
    order = cfg.get("date_order", "mdy")
    seen, docs = set(), []
    for ln in links:
        url = ln["url"].split("#")[0]
        if url in seen or not pdf_rx.search(url):
            continue
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).replace("+", " ")
        probe = f"{ln['text']} {name}"
        if not inc.search(probe) or exc.search(probe):
            continue
        seen.add(url)
        when = parse_doc_date(ln["text"], order) or parse_doc_date(name, order)
        if when is None and cfg.get("date_from_context"):  # opt-in: nearby text can belong to the previous row
            when = parse_doc_date(ln["context"][-80:], order)
        docs.append({**ln, "url": url, "file": name, "date": when, "kind": document_kind(ln["text"], url)})
    return docs


def expand_listing_urls(cfg: Dict[str, Any], since: Optional[date], until: Optional[date]) -> List[str]:
    out = []
    first_year = max(since.year if since else 2015, int(cfg.get("first_year", 2000)))
    last_year = min((until or date.today()).year, date.today().year + 1)
    for u in cfg.get("listing_urls") or []:
        if "{year}" in u:
            out += [u.replace("{year}", str(y)) for y in range(last_year, first_year - 1, -1)]
        else:
            out.append(u)
    return out


def _when(d: date, tz: str) -> datetime:
    return datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


async def discover_documents(
    client: httpx.AsyncClient, cfg: Dict[str, Any], *, since: Optional[date], until: Optional[date],
    allowed: Callable[[str], Any], report: Callable[[str], None] = lambda _m: None, stats: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Candidate documents in the window, newest first (no PDF downloaded yet)."""
    pages: List[str] = expand_listing_urls(cfg, since, until)
    follow = re.compile(cfg["follow_pattern"], re.I) if cfg.get("follow_pattern") else None
    visited, docs = set(), {}
    queue = [(u, 0) for u in pages]
    while queue:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if not await allowed(url):
            if stats is not None:
                stats.disallowed += 1
            continue
        report(f"listing {url[-70:]}")
        r = await client.get(url)
        if r.status_code == 404:
            continue  # e.g. a year page that doesn't exist (yet)
        r.raise_for_status()
        links = find_links(r.text, str(r.url))
        for d in select_documents(links, cfg):
            docs.setdefault(d["url"], d)
        if follow and depth == 0:
            queue += [(ln["url"].split("#")[0], 1) for ln in links if follow.search(ln["url"])]
    out = []
    for d in docs.values():
        if d["date"] is None:
            if stats is not None:
                stats.no_date += 1
            continue
        if (since and d["date"] < since) or (until and d["date"] >= until):
            if stats is not None:
                stats.out_of_window += 1
            continue
        out.append(d)
    out.sort(key=lambda d: d["date"], reverse=True)
    return out


async def discover_records(
    client: httpx.AsyncClient, cfg: Dict[str, Any], *, since: Optional[date], until: Optional[date],
    is_new: Callable[[List[str]], Any], allowed: Callable[[str], Any],
    report: Callable[[str], None] = lambda _m: None, stats: Optional[Any] = None,
) -> AsyncIterator:
    body = cfg.get("body") or ""
    tz = cfg.get("timezone", "America/Los_Angeles")
    docs = await discover_documents(client, cfg, since=since, until=until, allowed=allowed, report=report,
                                    stats=stats)
    if stats is not None:
        stats.discovered += len(docs)
    fresh = set(await is_new([d["url"] for d in docs]))
    for d in docs:
        if d["url"] not in fresh:
            continue
        if not await allowed(d["url"]):
            if stats is not None:
                stats.disallowed += 1
            continue
        report(f"{d['date'].isoformat()}: {d['kind']} {d['file'][:50]}")
        try:
            pages = await _pages(client, d["url"])
        except SourceThrottled:
            raise
        except Exception as exc:  # noqa: BLE001 - one unreadable PDF must not stop the crawl
            yield RecordError(d["url"], exc)
            continue
        label = d["text"] if 6 <= len(d["text"]) <= 120 and not re.fullmatch(r"(?i)pdf|download|view", d["text"]) \
            else d["kind"]
        head = f"{body} meeting document, {d['date'].isoformat()}: {label}".strip()
        yield RecordDoc(
            url=d["url"], title=f"{body}, {d['date'].isoformat()}: {label}"[:500], published_at=_when(d["date"], tz),
            text=f"{head}\n\n{join_pages(pages)}"[:MAX_TEXT_CHARS],
            doc_meta={"platform": "pdf_links", "body": body, "meeting_date": d["date"].isoformat(),
                      "kind": d["kind"], "link_text": d["text"][:200], "file": d["file"][:200], "pages": len(pages)},
        )
