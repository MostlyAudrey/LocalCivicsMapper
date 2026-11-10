"""CivicClerk official records: item staff reports, packet splitting, routine filtering, history, triage bypass.

Offline: a fake CivicClerk (OData API + blob PDFs + packet stream) behind httpx.MockTransport, real PostgreSQL.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from civic_tracker.crawler import civicclerk as cc
from civic_tracker.db import repositories as repo

ASYNC = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------- tiny text-PDF builder
def make_pdf(pages: list[str]) -> bytes:
    """Minimal valid PDF (Helvetica, one text line per input line) that pypdf can extract text from."""
    objs: list[bytes] = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, text in enumerate(pages):
        lines = []
        for ln in text.splitlines() or [""]:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"({esc}) Tj T*")
        stream = ("BT /F1 10 Tf 12 TL 40 760 Td " + " ".join(lines) + " ET").encode("latin-1", "replace")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


BODY = ("The Public Works Department recommends approval of the agreement. Fiscal impact: funded from the "
        "Water Enterprise fund. Background: the meters were installed between 2008 and 2011 and are near the "
        "end of their useful life. ")


def staff_report_pdf(subject: str) -> bytes:
    return make_pdf([
        "TO: THE HONORABLE MAYOR AND CITY COUNCIL\nSUBJECT: " + subject + "\n" + BODY * 2,
        BODY * 3,
    ])


# ---------------------------------------------------------------------------- pure helpers
def test_split_name_and_routine_rules():
    subject, rec = cc.split_name("SUBJECT: Approve Inspection Agreement with Webster Engineering<br><br>"
                                 "RECOMMENDATION FOR ACTION: Staff recommends that the City Council adopt "
                                 "Resolution No. ___")
    assert subject == "Approve Inspection Agreement with Webster Engineering"
    assert rec.startswith("Staff recommends that the City Council adopt")
    for routine in ("Council Meeting Minutes", "Approve Warrants", "Proclaim October 2026 as Breast Cancer "
                    "Awareness Month", "Childhood Cancer Awareness Month", "Long Range Calendar",
                    "General Public Comments", "Receive and File Treasurer Report"):
        assert cc.is_routine(routine), routine
    for real in ("Conditional Use Permit for Lindstrom's Auto Body", "Approval of Salary Schedule",
                 "Planning Commission Rules & Regulations"):
        assert not cc.is_routine(real), real


def test_pdf_text_and_packet_split():
    pages = cc.pdf_pages(make_pdf([
        "AGENDA\nCity Council April 5, 2016",
        "City of Woodland Legislation Text\nFile #: 16-427, Version: 1\nTO: THE HONORABLE MAYOR AND CITY COUNCIL\n"
        "SUBJECT: Long Range Calendar\nRecommendation for Action: Staff recommends receiving the calendar.",
        "City of Woodland Legislation Text\nFile #: 16-420, Version: 1\nTO: THE HONORABLE MAYOR AND CITY COUNCIL\n"
        "SUBJECT: Yolo Rail Realignment Economic Benefits Study\nRecommendation for Action: Staff recommends "
        "accepting the study.\n" + BODY,
        "",  # scanned attachment page -> dropped
        "Attachment A: study summary. " + BODY,
    ]))
    assert pages[3] == ""
    parts = cc.split_packet(pages)
    assert [p["file_no"] for p in parts] == ["16-427", "16-420"]
    rail = parts[1]
    assert rail["subject"] == "Yolo Rail Realignment Economic Benefits Study"
    assert rail["start_page"] == 3 and rail["end_page"] == 5
    assert "Attachment A" in rail["text"] and "Long Range" not in rail["text"]


def test_civicclerk_hosts_group_tenants_together():
    from civic_tracker.crawler.scraper import host_groups

    w = {"id": 1, "name": "W", "base_url": "https://woodlandca.portal.civicclerk.com",
         "scrape_config": {"method": "civicclerk", "tenant": "woodlandca"}}
    v = {"id": 2, "name": "V", "base_url": "https://wintersca.portal.civicclerk.com",
         "scrape_config": {"method": "civicclerk", "tenant": "wintersca"}}
    assert len(host_groups([w, v])) == 1  # shared PDF blob host -> one at a time


def test_stage2_prompt_marks_official_records():
    from civic_tracker.pipeline.stage2_llm import SYSTEM_PROMPT, build_user_prompt

    assert "OFFICIAL RECORD" in build_user_prompt("t", None, [], "x", "official_record")
    assert "OFFICIAL RECORDS" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------- fake CivicClerk
TENANT = "testcityca"
API = f"{TENANT}.api.civicclerk.com"


def _item(item_id, number, subject, report=True, children=None):
    return {
        "id": item_id, "agendaObjectItemOutlineNumber": number, "agendaObjectItemOutlineNumberFull": number,
        "agendaObjectItemName": f"SUBJECT: {subject}<br><br>RECOMMENDATION FOR ACTION: Staff recommends approval.",
        "reportsList": ([{"agendaObjItemReportName": "Staff Report",
                          "pdfMediaFullPath": f"https://{cc.BLOB_HOST}/stream/T/{item_id}.pdf?sig=abc"}]
                        if report else []),
        "attachmentsList": [{"fileName": "Site Plan"}], "childItems": children or [],
    }


EVENTS = [  # newest first, like the API ($orderby=startDateTime desc)
    {"id": 30, "agendaId": 300, "startDateTime": "2026-10-06T18:00:00Z", "categoryName": "City Council",
     "hasAgenda": True, "publishedFiles": []},
    {"id": 20, "agendaId": 200, "startDateTime": "2026-09-15T18:00:00Z", "categoryName": "City Council",
     "hasAgenda": True, "publishedFiles": []},
    {"id": 10, "agendaId": 100, "startDateTime": "2016-04-05T18:00:00Z", "categoryName": "City Council",
     "hasAgenda": True, "publishedFiles": [{"type": "Agenda Packet", "fileId": 1716}]},
]
MEETINGS = {
    300: {"items": [_item(1, "F", "CONSENT CALENDAR", report=False, children=[
        _item(31, "F.3", "Proclaim October 2026 as Breast Cancer Awareness Month"),
        _item(32, "F.9", "Approve Inspection Agreement with Webster Engineering"),
        _item(33, "F.10", "Samsara Vehicle Gateway Software License Agreement"),
    ])]},
    200: {"items": [_item(21, "G.11", "Triennial Public Health Goals Report for Water Quality")]},
    100: {"items": []},
}
PACKET = make_pdf([
    "AGENDA",
    "File #: 16-426, Version: 1\nTO: THE HONORABLE MAYOR AND CITY COUNCIL\nSUBJECT: Council Meeting Minutes\n" + BODY,
    "File #: 16-420, Version: 1\nTO: THE HONORABLE MAYOR AND CITY COUNCIL\n"
    "SUBJECT: Yolo Rail Realignment Economic Benefits Study\n" + BODY * 2,
])


def fake_civicclerk(log):
    import httpx

    def handler(req: httpx.Request):
        log.append(str(req.url))
        path = req.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: ClaudeBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
        if req.url.host == API and path == "/v1/Events":
            flt = req.url.params["$filter"]
            assert "categoryName eq 'City Council'" in flt
            lo = flt.split("startDateTime ge ")[1][:10] if "startDateTime ge" in flt else "0000"
            hi = flt.split("startDateTime lt ")[1][:10] if "startDateTime lt" in flt else "9999"
            evs = [e for e in EVENTS if lo <= e["startDateTime"][:10] < hi]
            skip = int(req.url.params.get("$skip", 0))
            return httpx.Response(200, json={"value": evs[skip:skip + int(req.url.params["$top"])]})
        if req.url.host == API and path.startswith("/v1/Meetings/GetMeetingFileStream"):
            return httpx.Response(200, content=PACKET, headers={"content-type": "application/pdf"})
        if req.url.host == API and path.startswith("/v1/Meetings/"):
            return httpx.Response(200, json=MEETINGS[int(path.rsplit("/", 1)[1])])
        if req.url.host == cc.BLOB_HOST:
            item_id = int(path.rsplit("/", 1)[1].split(".")[0])
            subject = {32: "Approve Inspection Agreement with Webster Engineering",
                       33: "Samsara Vehicle Gateway Software License Agreement",
                       31: "Breast Cancer Awareness Month",
                       21: "Triennial Public Health Goals Report for Water Quality"}[item_id]
            return httpx.Response(200, content=staff_report_pdf(subject), headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    return handler


@ASYNC
async def test_civicclerk_crawl_history_and_triage_bypass(pool, monkeypatch):
    import httpx

    from civic_tracker.crawler import polite, scraper
    from civic_tracker.pipeline.orchestrator import Orchestrator, RunStats
    from civic_tracker.pipeline.triage import Triage

    from .test_pipeline_e2e import _FakeClassifier, _components

    log: list[str] = []
    monkeypatch.setattr(scraper, "make_client", lambda delay=0: polite.PoliteClient(
        delay=0, transport=httpx.MockTransport(fake_civicclerk(log))))
    async with pool.connection() as conn:
        src = await repo.add_source(conn, "Test City Council", f"https://{TENANT}.portal.civicclerk.com",
                                    scrape_config={"method": "civicclerk", "tenant": TENANT,
                                                   "jurisdiction": "Testville"})

    # recent-style crawl from 2026-09-01: the two 2026 meetings (no upper bound)
    st = await scraper.crawl_source(pool, src, since=date(2026, 9, 1))
    assert st.method == "civicclerk" and st.errors == 0, st.error_samples
    assert st.inserted == 3 and st.filtered == 1, st  # proclamation skipped by subject
    assert not any("/31.pdf" in u for u in log)         # ... and its PDF never downloaded
    async with pool.connection() as conn:
        rows = {r["url"]: r for r in await (await conn.execute(
            "SELECT url, title, genre, yolo_relevance, triaged_at, published_at, full_text, doc_meta "
            "FROM TB_Article WHERE source_id=%s", (src["id"],))).fetchall()}
    url32 = f"https://{TENANT}.portal.civicclerk.com/event/30/overview#item-32"
    r = rows[url32]
    assert r["genre"] == "official_record" and r["yolo_relevance"] == "local" and r["triaged_at"] is not None
    assert r["title"] == "Testville City Council, 2026-10-06: Approve Inspection Agreement with Webster Engineering"
    assert r["published_at"].date() == date(2026, 10, 6)
    assert "Agenda item F.9" in r["full_text"] and "Water Enterprise fund" in r["full_text"]
    assert r["doc_meta"]["item_id"] == 32 and r["doc_meta"]["report_url"].endswith("/32.pdf")  # no signature
    assert r["doc_meta"]["attachments"] == ["Site Plan"]

    # second crawl: nothing new, and no PDF downloads for items already stored
    n_pdf = sum(cc.BLOB_HOST in u for u in log)
    st = await scraper.crawl_source(pool, src, since=date(2026, 9, 1))
    assert st.inserted == 0 and sum(cc.BLOB_HOST in u for u in log) == n_pdf

    # history walk: older packet-only meeting is split; minutes skipped
    async with pool.connection() as conn:
        src = await (await conn.execute("SELECT * FROM TB_Sources WHERE id=%s", (src["id"],))).fetchone()
    st = await scraper.crawl_source_history(pool, src, floor=date(2015, 1, 1), batch=20)
    assert st is not None and st.inserted == 1 and st.errors == 0, (st, st.error_samples)
    async with pool.connection() as conn:
        pk = await (await conn.execute(
            "SELECT url, title, doc_meta FROM TB_Article WHERE source_id=%s AND url LIKE %s",
            (src["id"], "%#packet-%"))).fetchall()
    assert len(pk) == 1 and pk[0]["url"].endswith("#packet-1716-16-420")
    assert pk[0]["title"] == "Testville City Council, 2016-04-05: Yolo Rail Realignment Economic Benefits Study"
    assert pk[0]["doc_meta"]["pages"] == [3, 3]
    n_packets = sum("GetMeetingFileStream" in u for u in log)
    st = await scraper.crawl_source_history(pool, dict(src, scrape_config=(await _cfg(pool, src["id"]))),
                                            floor=date(2015, 1, 1), batch=20)
    assert sum("GetMeetingFileStream" in u for u in log) == n_packets  # packet not downloaded again

    # processing: official records skip the LLM triage
    stage1, extractor, encoder = _components(False)
    clf = _FakeClassifier()
    orch = Orchestrator(pool, stage1=stage1, extractor=extractor, encoder=encoder, concurrency=1,
                        triage=Triage(clf))
    async with pool.connection() as conn:
        art = await (await conn.execute("SELECT * FROM TB_Article WHERE url=%s", (url32,))).fetchone()
    assert await orch._triage(dict(art), RunStats()) is True and clf.calls == 0

    # weighting: official records count like news
    async with pool.connection() as conn:
        vdef = (await (await conn.execute("SELECT pg_get_viewdef('vw_entity_event_weighted') AS d")).fetchone())["d"]
        await conn.execute("UPDATE TB_Article SET processing_status='SKIPPED' WHERE source_id=%s", (src["id"],))
    assert "official_record" in vdef


async def _cfg(pool, source_id):
    async with pool.connection() as conn:
        return (await (await conn.execute("SELECT scrape_config FROM TB_Sources WHERE id=%s",
                                          (source_id,))).fetchone())["scrape_config"]
