"""CivicPlus Agenda Center (https://<city site>/AgendaCenter) -> official records.

Used by Winters (cityofwinters.org: Planning Commission, Natural Resources Commission, committees, 2x2s) and
Woodland (cityofwoodland.gov: the boards/committees that are NOT on its CivicClerk portal). Both robots.txt
files allow /AgendaCenter (Winters asks for Crawl-delay 10, which the PoliteClient honours).

Page model (server-rendered, no API):
  * GET  /AgendaCenter                      -> categories (`id="catN"` ... `category-panel-N">Name`) and, per
                                               category, the years it has (`changeYear(YYYY, N, ...)` links).
  * POST /AgendaCenter/UpdateCategoryList   -> (year, catID) -> that year's rows, the same call the page makes.
  * rows: `<tr class="catAgendaRow">` with `ViewFile/Agenda/_MMDDYYYY-ID` (agenda PDF), optional
    `ViewFile/Minutes/_MMDDYYYY-ID` and optional `...?packet=true` (agenda packet).

One record per meeting document: the agenda (its packet when there is one - packets carry the staff reports)
and, separately, the minutes (motions and votes). Text PDFs only; scanned pages are dropped.
"""
from __future__ import annotations

import html as _html
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import httpx

from civic_tracker.crawler.civicclerk import MAX_TEXT_CHARS, RecordDoc, RecordError, _pages, join_pages
from civic_tracker.crawler.polite import SourceThrottled

log = logging.getLogger(__name__)

_CAT = re.compile(r'id="cat(\d+)".{0,600}?aria-controls="category-panel-\1"[^>]*>\s*([^<]{2,120}?)\s*<', re.S)
_YEAR = re.compile(r"changeYear\(\s*(\d{4})\s*,\s*(\d+)")
_ROW = re.compile(r'<tr[^>]*class="catAgendaRow"[^>]*>(.*?)</tr>', re.S)
_FILE = re.compile(r'href="(/AgendaCenter/ViewFile/(Agenda|Minutes)/_(\d{8})-(\d+)(\?packet=true)?)"', re.I)
_TITLE = re.compile(r'href="/AgendaCenter/ViewFile/Agenda/_\d{8}-\d+"[^>]*>\s*([^<]+?)\s*</a>', re.S)


def parse_index(page: str) -> Tuple[Dict[int, str], Dict[int, List[int]]]:
    """(categories id -> name, years available per category id, newest first)."""
    cats = {int(i): _html.unescape(n).strip() for i, n in _CAT.findall(page)}
    years: Dict[int, set] = {c: set() for c in cats}
    for y, c in _YEAR.findall(page):
        years.setdefault(int(c), set()).add(int(y))
    for m in re.finditer(r'id="category-panel-(\d+)"(.*?)</table>', page, re.S):  # rows shown = current year
        for f in _FILE.finditer(m.group(2)):
            years.setdefault(int(m.group(1)), set()).add(int(f.group(3)[4:]))
    return cats, {c: sorted(ys, reverse=True) for c, ys in years.items()}


def parse_rows(fragment: str) -> List[Dict[str, Any]]:
    """Meetings in an UpdateCategoryList response (or a category panel), newest first."""
    out = []
    for row in _ROW.findall(fragment):
        files = {}
        mdy = None
        for path, kind, d, fid, packet in _FILE.findall(row):
            key = "packet" if packet else kind.lower()
            files.setdefault(key, path)
            mdy = mdy or d
        if not mdy or "agenda" not in files and "packet" not in files and "minutes" not in files:
            continue
        # several links point at the agenda (title, "PDF", "HTML" icons): the longest text is the title
        titles = [_html.unescape(re.sub(r"\s+", " ", t)).strip() for t in _TITLE.findall(row)]
        out.append({"date": date(int(mdy[4:]), int(mdy[:2]), int(mdy[2:4])),
                    "title": max(titles, key=len) if titles else "", "files": files})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def wanted_categories(cats: Dict[int, str], cfg: Dict[str, Any]) -> Dict[int, str]:
    """scrape_config.categories: list of names (case-insensitive) or ids; default all. `exclude_categories` too."""
    want = cfg.get("categories")
    out = dict(cats)
    if want:
        keys = {str(w).lower() for w in want}
        out = {i: n for i, n in cats.items() if str(i) in keys or n.lower() in keys}
    skip = {str(w).lower() for w in cfg.get("exclude_categories") or []}
    return {i: n for i, n in out.items() if str(i) not in skip and n.lower() not in skip}


def _when(d: date, tz: str) -> datetime:
    return datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def record_title(city: str, body: str, d: date, kind: str) -> str:
    body_label = body if body.lower().startswith(city.lower()) else f"{city} {body}"
    return f"{body_label}, {d.isoformat()}: {'Minutes' if kind == 'minutes' else 'Agenda'}"[:500]


async def discover_records(
    client: httpx.AsyncClient,
    base_url: str,
    cfg: Dict[str, Any],
    *,
    since: Optional[date],
    until: Optional[date],
    is_new: Callable[[List[str]], Any],
    allowed: Callable[[str], Any],
    report: Callable[[str], None] = lambda _m: None,
    stats: Optional[Any] = None,
) -> AsyncIterator[Union[RecordDoc, RecordError]]:
    """Yield new agenda / minutes records, category by category, newest year first."""
    # base_url may be the site root or ".../AgendaCenter" (TB_Sources.base_url is unique and the site root is
    # often already taken by the city's news source)
    base = re.sub(r"/AgendaCenter/?$", "", base_url.rstrip("/"), flags=re.I)
    city = cfg.get("jurisdiction") or ""
    tz = cfg.get("timezone", "America/Los_Angeles")
    idx_url = f"{base}/AgendaCenter"
    if not await allowed(idx_url):
        if stats is not None:
            stats.disallowed += 1
        return
    r = await client.get(idx_url)
    r.raise_for_status()
    cats, years = parse_index(r.text)
    for cid, body in wanted_categories(cats, cfg).items():
        for year in years.get(cid, []):
            if since and year < since.year:
                break
            if until and year > until.year:
                continue
            report(f"{body} {year}: listing")
            lr = await client.post(f"{base}/AgendaCenter/UpdateCategoryList", data={"year": year, "catID": cid},
                                   headers={"X-Requested-With": "XMLHttpRequest"})
            lr.raise_for_status()
            rows = [m for m in parse_rows(lr.text)
                    if (not since or m["date"] >= since) and (not until or m["date"] < until)]
            docs = []
            for m in rows:
                agenda = m["files"].get("packet") or m["files"].get("agenda")
                if agenda:
                    docs.append(("agenda", m, base + agenda))
                if cfg.get("include_minutes", True) and m["files"].get("minutes"):
                    docs.append(("minutes", m, base + m["files"]["minutes"]))
            if stats is not None:
                stats.discovered += len(docs)
            fresh = set(await is_new([u for _, _, u in docs]))
            for kind, m, url in docs:
                if url not in fresh:
                    continue
                if not await allowed(url):
                    if stats is not None:
                        stats.disallowed += 1
                    continue
                report(f"{body} {m['date'].isoformat()}: {kind}")
                try:
                    pages = await _pages(client, url)
                except SourceThrottled:
                    raise
                except Exception as exc:  # noqa: BLE001 - one unreadable PDF must not stop the crawl
                    yield RecordError(url, exc)
                    continue
                head = (f"{city} {body} meeting, {m['date'].isoformat()}. "
                        f"{'Minutes' if kind == 'minutes' else 'Agenda'}: {m['title']}").strip()
                yield RecordDoc(
                    url=url, title=record_title(city, body, m["date"], kind), published_at=_when(m["date"], tz),
                    text=f"{head}\n\n{join_pages(pages)}"[:MAX_TEXT_CHARS],
                    doc_meta={"platform": "civicplus_agendacenter", "body": body, "category_id": cid,
                              "meeting_date": m["date"].isoformat(), "kind": kind,
                              "packet": "packet=true" in url, "pages": len(pages), "listing_title": m["title"]},
                )
