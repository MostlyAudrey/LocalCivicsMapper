"""Generic PDF-links crawler: date parsing, document selection, listing expansion."""
from datetime import date

from civic_tracker.crawler import pdflinks as pl


def test_parse_doc_date_formats():
    assert pl.parse_doc_date("LAFCo Agenda 02.23.23.pdf") == date(2023, 2, 23)
    assert pl.parse_doc_date("10.28.26-Commission-Agenda-Packet.pdf") == date(2026, 10, 28)
    assert pl.parse_doc_date("Minutes October 28, 2026") == date(2026, 10, 28)
    assert pl.parse_doc_date("2024-05-14 Board Agenda") == date(2024, 5, 14)
    assert pl.parse_doc_date("BoardPackage_20261103.pdf") == date(2026, 11, 3)
    assert pl.parse_doc_date("Agenda_10282026.pdf") == date(2026, 10, 28)
    assert pl.parse_doc_date("Agenda+12.18.19+AUDIO") == date(2019, 12, 18)
    assert pl.parse_doc_date("Resolution 2026-12") is None


def test_select_documents_and_kinds():
    page = """
      <p>October 28, 2026</p><a href="/up/10.28.26-Commission-Agenda-Packet.pdf">Agenda &amp; Packet</a>
      <a href="/up/Minutes-2026-09-09.pdf">Minutes</a>
      <a href="/up/Volunteer-Flyer-2026.pdf">Volunteer flyer</a>
      <a href="/about">About</a>
      <a href="/up/Regular-Board-Meeting.pdf">Regular Board Meeting Agenda</a>"""
    docs = pl.select_documents(pl.find_links(page, "https://agency.example/meetings"), {})
    by_file = {d["file"]: d for d in docs}
    assert "Volunteer-Flyer-2026.pdf" not in by_file                   # excluded
    assert by_file["10.28.26-Commission-Agenda-Packet.pdf"]["kind"] == "Agenda Packet"
    assert by_file["Minutes-2026-09-09.pdf"]["date"] == date(2026, 9, 9)
    assert by_file["10.28.26-Commission-Agenda-Packet.pdf"]["url"] == \
        "https://agency.example/up/10.28.26-Commission-Agenda-Packet.pdf"
    # no date in text or file name: undated (skipped later) unless date_from_context is enabled
    assert by_file["Regular-Board-Meeting.pdf"]["date"] is None
    assert pl.select_documents(pl.find_links(page, "https://a.example/"), {"exclude": "packet"}) and \
        all("Packet" not in d["kind"] for d in pl.select_documents(pl.find_links(page, "https://a.example/"),
                                                                   {"exclude": "packet|flyer"}))


def test_expand_listing_urls_years():
    cfg = {"listing_urls": ["https://x.example/Board/{year}-Meetings/", "https://x.example/all"]}
    urls = pl.expand_listing_urls(cfg, date(2024, 1, 1), date(2026, 6, 1))
    assert urls == ["https://x.example/Board/2026-Meetings/", "https://x.example/Board/2025-Meetings/",
                    "https://x.example/Board/2024-Meetings/", "https://x.example/all"]
