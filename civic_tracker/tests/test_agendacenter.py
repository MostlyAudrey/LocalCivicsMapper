"""CivicPlus Agenda Center crawler: index + per-year listing -> agenda/packet and minutes records."""
from datetime import date

import httpx
import pytest

from civic_tracker.crawler import agendacenter as ac
from civic_tracker.db import repositories as repo

ASYNC = pytest.mark.asyncio(loop_scope="session")

INDEX = """<html><body>
<div id="cat9"><h2 aria-controls="category-panel-9">Planning Commission</h2>
 <a href="javascript:changeYear(2025, 9,'a0')">2025</a><a href="javascript:changeYear(2024, 9, 'a1')">2024</a></div>
<div id="category-panel-9"><table><tbody></tbody></table></div>
<div id="cat3"><h2 aria-controls="category-panel-3">Climate Action Committee</h2>
 <a href="javascript:changeYear(2025, 3,'a0')">2025</a></div>
<div id="category-panel-3"><table><tbody></tbody></table></div>
</body></html>"""


def year_rows(year):
    if year == 2025:
        return """<tr class="catAgendaRow"><td><h3 id="h411252025-164"></h3>
          <p><a href="/AgendaCenter/ViewFile/Agenda/_11252025-164">Planning Commission Agenda November 25, 2025</a></p></td>
          <td class="minutes"><a href="/AgendaCenter/ViewFile/Minutes/_11252025-164">m</a></td>
          <td><a href="/AgendaCenter/ViewFile/Agenda/_11252025-164?packet=true">Packet</a>
              <a href="/AgendaCenter/ViewFile/Agenda/_11252025-164">PDF</a></td></tr>"""
    return """<tr class="catAgendaRow"><td><p><a href="/AgendaCenter/ViewFile/Agenda/_03122024-120">Planning
          Commission Agenda March 12, 2024</a></p></td><td class="minutes"></td></tr>"""


def make_pdf(text: str) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 10 Tf 40 700 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(stream)
    import io
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_parse_index_and_rows():
    cats, years = ac.parse_index(INDEX)
    assert cats == {9: "Planning Commission", 3: "Climate Action Committee"}
    assert years[9] == [2025, 2024]
    rows = ac.parse_rows(year_rows(2025))
    assert rows[0]["date"] == date(2025, 11, 25)
    assert rows[0]["title"] == "Planning Commission Agenda November 25, 2025"  # not the "PDF" icon link
    assert set(rows[0]["files"]) == {"agenda", "minutes", "packet"}
    assert ac.wanted_categories(cats, {"categories": ["planning commission"]}) == {9: "Planning Commission"}


@ASYNC
async def test_agendacenter_crawl_and_history(pool, monkeypatch):
    from civic_tracker.crawler import polite, scraper

    fetched = []
    body = "The Planning Commission approved the Grant Avenue mixed-use project on a 4-1 vote. " * 4

    def handler(req: httpx.Request):
        fetched.append((req.method, req.url.path, req.url.query.decode()))
        if req.url.path.endswith("robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /Search\n")
        if req.url.path == "/AgendaCenter":
            return httpx.Response(200, html=INDEX)
        if req.url.path == "/AgendaCenter/UpdateCategoryList":
            form = dict(x.split("=") for x in req.content.decode().split("&"))
            return httpx.Response(200, html=year_rows(int(form["year"])) if form["catID"] == "9" else "")
        if "/ViewFile/" in req.url.path:
            return httpx.Response(200, content=make_pdf(body + req.url.path), headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    monkeypatch.setattr(scraper, "make_client", lambda delay=0: polite.PoliteClient(
        delay=0, transport=httpx.MockTransport(handler)))
    cfg = {"method": "agendacenter", "jurisdiction": "Winters", "categories": ["Planning Commission"]}
    async with pool.connection() as conn:
        src = await repo.add_source(conn, "Winters Agenda Center", "https://ac.example", scrape_config=cfg)

    st = await scraper.crawl_source(pool, src, since=date(2025, 1, 1), backfill=True)
    assert st.inserted == 2, (st, fetched)  # packet (preferred over the plain agenda) + minutes
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT url, title, genre, doc_meta FROM TB_Article WHERE source_id=%s ORDER BY url", (src["id"],))).fetchall()
    assert {r["genre"] for r in rows} == {"official_record"}
    assert any(r["url"].endswith("?packet=true") for r in rows) and any("/Minutes/" in r["url"] for r in rows)
    assert rows[0]["title"].startswith("Winters Planning Commission, 2025-11-25")

    # history: walks back to the 2024 meeting; the second pass downloads nothing new
    st = await scraper.crawl_source_history(pool, src, floor=date(2015, 1, 1), batch=10)
    assert st.inserted == 1, st
    n_pdf = sum(1 for f in fetched if "/ViewFile/" in f[1])
    st = await scraper.crawl_source(pool, src, since=date(2024, 1, 1), backfill=True)
    assert st.inserted == 0 and sum(1 for f in fetched if "/ViewFile/" in f[1]) == n_pdf
