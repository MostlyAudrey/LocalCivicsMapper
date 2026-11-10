"""CivicClerk (city council agendas) -> one official record per staff-reported agenda item.

Used by Woodland (tenant `woodlandca`) and Winters (`wintersca`). Both publish a public OData API at
https://<tenant>.api.civicclerk.com/v1 (robots.txt: `User-agent: *  Allow: /`). Everything here goes through the
caller's PoliteClient (one request at a time, randomized spacing, Retry-After) and robots.txt checks.

Two shapes of meeting:
  * itemized (Woodland: seen from 2021 on; 2016 is packet-only): /Meetings/{agendaId} lists agenda items; each item's
    `reportsList` holds the generated "Staff Report" PDF (Azure blob, short-lived signed URL). One record per item.
  * packet-only (older imported meetings): only a whole "Agenda Packet" PDF. It is split at each staff report's
    cover page ("TO: THE HONORABLE MAYOR AND CITY COUNCIL ... SUBJECT: ..."). One record per report.

Text PDFs only (no OCR); scanned pages are dropped. Routine items (minutes, warrants, proclamations,
receive-and-file, ...) are skipped by subject.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple, Union

import httpx

from civic_tracker.crawler.polite import SourceThrottled

log = logging.getLogger(__name__)

BLOB_HOST = "civicclerk.blob.core.windows.net"
DEFAULT_BODIES = ["City Council"]
MAX_TEXT_CHARS = 40_000
MAX_PDF_BYTES = 150 * 1024 * 1024
PDF_TIMEOUT = httpx.Timeout(180.0, connect=30.0)
PAGE_SIZE = 25
MIN_PAGE_CHARS = 50  # below this a page is scanned / blank

ROUTINE_SUBJECT = re.compile(
    r"\bminutes\b|\bwarrants?\b|\bclaims?\s+(?:register|list|report)\b|\bproclaim|\bproclamation"
    r"|awareness\s+(?:month|week|day)|\breceive\s+and\s+file\b|long\s+range\s+calendar|public\s+comments?"
    r"|\bclosed\s+session\b|\broll\s+call\b|\bpledge\s+of\s+allegiance\b|\badjourn",
    re.IGNORECASE,
)
REPORT_START = re.compile(r"TO:\s*(?:THE\s+)?HONORABLE\s+MAYOR", re.IGNORECASE)
SUBJECT_RE = re.compile(r"SUBJECT:\s*(.+?)(?=\s*(?:RECOMMENDATION|Recommendation for Action|$))",
                        re.IGNORECASE | re.DOTALL)
RECOMMENDATION_RE = re.compile(r"RECOMMENDATION(?:\s+FOR\s+ACTION)?\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
FILE_NO_RE = re.compile(r"File\s*#:\s*([\w-]+)")


@dataclass
class RecordDoc:
    url: str
    title: str
    published_at: datetime
    text: str
    doc_meta: Dict[str, Any] = field(default_factory=dict)


def api_base(tenant: str) -> str:
    return f"https://{tenant}.api.civicclerk.com/v1"


def portal_base(tenant: str) -> str:
    return f"https://{tenant}.portal.civicclerk.com"


def tenant_hosts(tenant: str) -> List[str]:
    return [f"{tenant}.api.civicclerk.com", f"{tenant}.portal.civicclerk.com", BLOB_HOST]


def clean_html(value: Optional[str]) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text)).replace("­", "").replace("​", "")
    return re.sub(r"[ \t\xa0]+", " ", text).strip()


def split_name(name_html: Optional[str]) -> Tuple[str, str]:
    """Agenda item name -> (subject, recommendation)."""
    text = clean_html(name_html)
    m = SUBJECT_RE.search(text)
    subject = re.sub(r"\s+", " ", m.group(1)).strip() if m else re.sub(r"\s+", " ", text).strip()
    r = RECOMMENDATION_RE.search(text)
    rec = re.sub(r"\s+", " ", r.group(1)).strip() if r else ""
    return subject[:300], rec


def is_routine(subject: str) -> bool:
    return bool(ROUTINE_SUBJECT.search(subject or ""))


def parse_meeting_dt(value: str) -> datetime:
    # CivicClerk stores local wall-clock time with a Z suffix; the date part is the local meeting date.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def pdf_pages(data: bytes) -> List[str]:
    """Text per page ('' for scanned/blank pages). Raises on non-PDF input."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for p in reader.pages:
        try:
            t = p.extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not lose the document
            t = ""
        pages.append(t if len(t.strip()) >= MIN_PAGE_CHARS else "")
    return pages


def join_pages(pages: List[str], max_chars: int = MAX_TEXT_CHARS) -> str:
    text = "\n".join(p.strip() for p in pages if p)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text[:max_chars]


def split_packet(pages: List[str]) -> List[Dict[str, Any]]:
    """Split an agenda packet into staff reports at their cover pages. Each report runs until the next cover
    page (attachments included, scanned pages dropped)."""
    starts = [i for i, t in enumerate(pages)
              if t and REPORT_START.search(t[:700]) and "SUBJECT:" in t[:2000].upper()]
    out = []
    for n, s in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(pages)
        head = pages[s][:2500]
        seg = head[head.upper().find("SUBJECT:") + len("SUBJECT:"):]
        m = re.search(r"Recommendation", seg[:500], re.IGNORECASE)
        # wrapped subjects run until "Recommendation for Action"; without one, keep the first line only
        subject = re.sub(r"\s+", " ", seg[:m.start()] if m else seg.strip().split("\n")[0]).strip()[:300]
        r = RECOMMENDATION_RE.search(seg[m.start():m.start() + 3000]) if m else None
        rec = re.sub(r"\s+", " ", r.group(1)).strip()[:1500] if r else ""
        fno = FILE_NO_RE.search(head[:600])
        out.append({"start_page": s + 1, "end_page": end, "subject": subject, "recommendation": rec,
                    "file_no": fno.group(1) if fno else None, "text": join_pages(pages[s:end])})
    return out


def _odata_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def list_events(client: httpx.AsyncClient, tenant: str, bodies: List[str], since: Optional[date],
                      until: Optional[date]) -> AsyncIterator[Dict[str, Any]]:
    """Meetings of the given bodies with since <= date <= until (either open), newest first."""
    flt = ["(" + " or ".join(f"categoryName eq {_odata_str(b)}" for b in bodies) + ")"]
    if since:
        flt.append(f"startDateTime ge {since.isoformat()}T00:00:00Z")
    if until:
        flt.append(f"startDateTime lt {(until + timedelta(days=1)).isoformat()}T00:00:00Z")
    skip = 0
    while True:
        r = await client.get(f"{api_base(tenant)}/Events", params={
            "$filter": " and ".join(flt), "$orderby": "startDateTime desc", "$top": PAGE_SIZE, "$skip": skip})
        r.raise_for_status()
        batch = r.json().get("value") or []
        for ev in batch:
            yield ev
        if len(batch) < PAGE_SIZE:
            return
        skip += PAGE_SIZE


def flatten_items(items: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in items or []:
        out.append(it)
        out.extend(flatten_items(it.get("childItems")))
    return out


def staff_report(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    reports = [r for r in item.get("reportsList") or [] if r.get("pdfMediaFullPath")]
    if not reports:
        return None
    named = [r for r in reports if "staff report" in (r.get("agendaObjItemReportName") or "").lower()]
    return (named or reports)[0]


def packet_file(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for f in event.get("publishedFiles") or []:
        if (f.get("type") or "").lower() == "agenda packet" and f.get("fileId"):
            return f
    return None


async def fetch_pdf(client: httpx.AsyncClient, url: str) -> bytes:
    r = await client.get(url, timeout=PDF_TIMEOUT)
    r.raise_for_status()
    if len(r.content) > MAX_PDF_BYTES:
        raise ValueError(f"PDF too large ({len(r.content) // 1_000_000} MB)")
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"not a PDF ({r.headers.get('content-type')})")
    return r.content


def minutes_file(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The meeting's published minutes (publishedFiles type "Minutes"), if posted yet."""
    for f in event.get("publishedFiles") or []:
        if (f.get("type") or "").lower() == "minutes" and f.get("fileId"):
            return f
    return None


def minutes_url(tenant: str, event_id: int, file_id: int) -> str:
    return f"{portal_base(tenant)}/event/{event_id}/overview#minutes-{file_id}"


def item_url(tenant: str, event_id: int, item_id: int) -> str:
    return f"{portal_base(tenant)}/event/{event_id}/overview#item-{item_id}"


def packet_url(tenant: str, event_id: int, file_id: int, key: str) -> str:
    return f"{portal_base(tenant)}/event/{event_id}/overview#packet-{file_id}-{key}"


def record_title(city: str, body: str, when: datetime, subject: str) -> str:
    body_label = body if body.lower().startswith(city.lower()) else f"{city} {body}"
    return f"{body_label}, {when.date().isoformat()}: {subject}"[:500]


def record_text(city: str, body: str, when: datetime, number: str, subject: str, rec: str, report: str) -> str:
    head = f"{city} {body} meeting, {when.date().isoformat()}. Agenda item {number}: {subject}".replace("  ", " ")
    parts = [head]
    if rec:
        parts.append(f"Recommendation: {rec}")
    parts.append(report)
    return "\n\n".join(p for p in parts if p)[:MAX_TEXT_CHARS]


async def discover_records(
    client: httpx.AsyncClient,
    cfg: Dict[str, Any],
    *,
    since: Optional[date],
    until: Optional[date],
    is_new: Callable[[List[str]], Any],
    known_prefix: Callable[[str], Any],
    allowed: Callable[[str], Any],
    report: Callable[[str], None] = lambda _m: None,
    stats: Optional[Any] = None,
) -> AsyncIterator[Union[RecordDoc, "RecordError"]]:
    """Yield new official records meeting by meeting (newest first), or RecordError for unreadable ones.
    `is_new(urls)` -> awaitable list of the URLs not stored yet (known items cost no PDF download);
    `known_prefix(p)` -> awaitable bool, any stored URL starts with p; `allowed(url)` -> robots.txt check."""
    tenant = cfg["tenant"]
    city = cfg.get("jurisdiction") or tenant
    bodies = cfg.get("bodies") or DEFAULT_BODIES
    async for ev in list_events(client, tenant, bodies, since, until):
        if not ev.get("hasAgenda") or not ev.get("agendaId"):
            continue
        when = parse_meeting_dt(ev["startDateTime"])
        body = ev.get("categoryName") or ev.get("eventName") or "City Council"
        label = f"{body} {when.date().isoformat()}"
        report(f"{label}: reading agenda")
        r = await client.get(f"{api_base(tenant)}/Meetings/{ev['agendaId']}")
        if r.status_code == 404:
            continue
        r.raise_for_status()
        items = flatten_items(r.json().get("items"))
        with_reports = [(it, staff_report(it)) for it in items]
        with_reports = [(it, rep) for it, rep in with_reports if rep]
        base_meta = {"platform": "civicclerk", "tenant": tenant, "body": body, "event_id": ev["id"],
                     "agenda_id": ev["agendaId"], "meeting_date": when.date().isoformat()}

        # the meeting's official minutes: the only source of roll-call votes (v1.8)
        mf = minutes_file(ev)
        if mf and cfg.get("include_minutes", True):
            murl = minutes_url(tenant, ev["id"], mf["fileId"])
            if stats is not None:
                stats.discovered += 1
            if await is_new([murl]):
                file_url = f"{api_base(tenant)}/Meetings/GetMeetingFileStream(fileId={mf['fileId']},plainText=false)"
                if await allowed(file_url):
                    report(f"{label}: minutes")
                    try:
                        pages = await _pages(client, file_url)
                    except SourceThrottled:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        yield RecordError(murl, exc)
                    else:
                        yield RecordDoc(
                            url=murl, title=f"{record_title(city, body, when, 'Minutes')}",
                            published_at=when,
                            text=f"{city} {body} meeting, {when.date().isoformat()}. Official minutes.\n\n"
                                 f"{join_pages(pages)}"[:MAX_TEXT_CHARS],
                            doc_meta={**base_meta, "kind": "minutes", "minutes_file_id": mf["fileId"],
                                      "pages": len(pages)},
                        )
                elif stats is not None:
                    stats.disallowed += 1

        if with_reports:
            wanted = []
            for it, rep in with_reports:
                subject, rec = split_name(it.get("agendaObjectItemName"))
                if is_routine(subject):
                    if stats is not None:
                        stats.filtered += 1
                    continue
                wanted.append((item_url(tenant, ev["id"], it["id"]), it, rep, subject, rec))
            if stats is not None:
                stats.discovered += len(wanted)
            fresh = set(await is_new([w[0] for w in wanted]))
            for n, (url, it, rep, subject, rec) in enumerate([w for w in wanted if w[0] in fresh], 1):
                pdf_url = rep["pdfMediaFullPath"]
                if not await allowed(pdf_url):
                    if stats is not None:
                        stats.disallowed += 1
                    continue
                report(f"{label}: staff report {n}/{len(fresh)}: {subject[:50]}")
                try:
                    pages = await _pages(client, pdf_url)
                except SourceThrottled:
                    raise
                except Exception as exc:  # noqa: BLE001 - one unreadable PDF must not stop the crawl
                    yield RecordError(url, exc)
                    continue
                number = (it.get("agendaObjectItemOutlineNumberFull") or it.get("agendaObjectItemOutlineNumber")
                          or "").strip(". ")
                yield RecordDoc(
                    url=url, title=record_title(city, body, when, subject), published_at=when,
                    text=record_text(city, body, when, number, subject, rec, join_pages(pages)),
                    doc_meta={**base_meta, "kind": "staff_report", "item_id": it["id"], "item_number": number,
                              "report_url": pdf_url.split("?")[0], "pages": len(pages),
                              "attachments": [a.get("fileName") for a in it.get("attachmentsList") or []][:20]},
                )
            continue

        pf = packet_file(ev)
        if not pf:
            continue
        pkt_url = f"{api_base(tenant)}/Meetings/GetMeetingFileStream(fileId={pf['fileId']},plainText=false)"
        # Packet-only meeting: skip the (large) download when reports from it are already stored.
        prefix = packet_url(tenant, ev["id"], pf["fileId"], "")
        if await known_prefix(prefix):
            continue
        if not await allowed(pkt_url):
            if stats is not None:
                stats.disallowed += 1
            continue
        report(f"{label}: downloading agenda packet")
        try:
            pages = await _pages(client, pkt_url)
        except SourceThrottled:
            raise
        except Exception as exc:  # noqa: BLE001
            yield RecordError(prefix, exc)
            continue
        parts = split_packet(pages)
        kept = []
        for p in parts:
            if is_routine(p["subject"]):
                if stats is not None:
                    stats.filtered += 1
                continue
            key = p["file_no"] or f"p{p['start_page']}"
            kept.append((packet_url(tenant, ev["id"], pf["fileId"], key), p))
        if stats is not None:
            stats.discovered += len(kept)
        fresh = set(await is_new([u for u, _ in kept]))
        for url, p in kept:
            if url not in fresh:
                continue
            yield RecordDoc(
                url=url, title=record_title(city, body, when, p["subject"]), published_at=when,
                text=record_text(city, body, when, p["file_no"] or "", p["subject"], p["recommendation"], p["text"]),
                doc_meta={**base_meta, "kind": "packet_staff_report", "packet_file_id": pf["fileId"],
                          "file_no": p["file_no"], "pages": [p["start_page"], p["end_page"]]},
            )


async def _pages(client: httpx.AsyncClient, url: str) -> List[str]:
    data = await fetch_pdf(client, url)
    return await asyncio.to_thread(pdf_pages, data)  # pypdf is CPU-bound; keep the event loop free


@dataclass
class RecordError:
    """A record that could not be read (yielded, not raised, so the crawl continues)."""
    url: str
    exc: BaseException

    def __str__(self) -> str:
        return f"{self.url}: {type(self.exc).__name__}: {self.exc}"
