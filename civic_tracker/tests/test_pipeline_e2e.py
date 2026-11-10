"""End-to-end pipeline test on the Davis Vanguard ICE-stop scenario.

Offline (default): fakes for coref/NER/LLM/embeddings, real PostgreSQL (TEST_DATABASE_URL).
Live (--run-live): real fastcoref + GLiNER + DeepSeek V4-flash + Qwen3-Embedding-4B.
"""
from __future__ import annotations

import pytest

from civic_tracker.db import repositories as repo
from civic_tracker.pipeline.encoder import format_qwen3_input, truncate_and_l2_normalize
from civic_tracker.pipeline.orchestrator import Orchestrator
from civic_tracker.pipeline.resolver import choose_event_match
from civic_tracker.pipeline.stage1_nlp import Stage1Processor, annotate_coreferences
from civic_tracker.pipeline.stage2_llm import parse_extraction

from . import fixtures as fx
from .conftest import FakeEncoder, fake_coref, fake_ner, regex_sentences

ASYNC = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Pure unit tests (no DB)
# ---------------------------------------------------------------------------
def test_qwen3_formatting_and_mrl():
    assert format_qwen3_input("  a  b ", "SEARCH_DOCUMENT") == "a b"
    assert format_qwen3_input("x", "EVENT_DEDUP").startswith("Instruct: ") and "\nQuery: x" in format_qwen3_input("x", "EVENT_DEDUP")
    v = truncate_and_l2_normalize([3.0, 4.0, 99.0], 2)
    assert v == pytest.approx([0.6, 0.8])


def test_coref_annotation():
    text = "Gloria Ravel spoke. She apologized."
    out = annotate_coreferences(text, [[(0, 14), (22, 25)]])
    assert out == "Gloria Ravel spoke. She [Gloria Ravel] apologized."


def test_event_merge_tiers():
    assert choose_event_match([{"id": 1, "cosine_sim": 0.85, "existing_entity_ids": []}], set()) == 1
    tier2 = [{"id": 2, "cosine_sim": 0.78, "existing_entity_ids": [10, 11, 12]}]
    assert choose_event_match(tier2, {10, 11}) == 2
    assert choose_event_match(tier2, {10}) is None
    assert choose_event_match([], {1}) is None


def test_parse_extraction_strips_think_and_fences():
    raw = '<think>hmm</think>```json\n{"affiliations": [], "events": []}\n```'
    assert parse_extraction(raw).events == []


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------
class FakeExtractor:
    async def extract(self, title, published_at, candidate_entities, evidence_bundle, on_call=None, genre=None,
                      jurisdiction=None, **_kw):
        return fx.MOCK_RESULT_1 if title == fx.ARTICLE_1_TITLE else fx.MOCK_RESULT_2


def _components(run_live: bool):
    if run_live:
        from civic_tracker.pipeline.encoder import LocalEncoderClient
        from civic_tracker.pipeline.stage2_llm import DeepSeekExtractor

        return Stage1Processor(), DeepSeekExtractor(), LocalEncoderClient()
    return (
        Stage1Processor(coref_fn=fake_coref, ner_fn=fake_ner, sentence_fn=regex_sentences),
        FakeExtractor(),
        FakeEncoder(),
    )


async def _insert(conn, source_id, url, title, published, text):
    return await repo.insert_article(
        conn, source_id=source_id, url=url, title=title, author=None, published_at=published,
        full_text=text, content_hash=repo_hash(text),
    )


def repo_hash(text):
    from civic_tracker.crawler.scraper import sha256_text

    return sha256_text(text)


async def _entity_present(conn, name: str):
    row = await repo.find_entity_by_name(conn, name)
    if row is None and name == "ICE":
        row = await repo.find_entity_by_name(conn, "Immigration and Customs Enforcement")
    return row


@ASYNC
async def test_stage1_evidence_bundle_contains_all_targets(run_live):
    stage1, _, _ = _components(run_live)
    res = await stage1.aprocess(fx.ARTICLE_1_TEXT)
    assert not res.skipped
    for name, _ in fx.ALL_9:
        assert name in res.evidence_bundle, f"{name} missing from evidence bundle"
    if not run_live:
        assert "She [Robin Hale] alleged" in res.resolved_text


@ASYNC
async def test_full_pipeline_dedup_and_coalitions(pool, run_live):
    stage1, extractor, encoder = _components(run_live)
    orch = Orchestrator(pool, stage1=stage1, extractor=extractor, encoder=encoder, concurrency=2)

    async with pool.connection() as conn:
        src = await repo.add_source(conn, "Davis Vanguard", "https://davisvanguard.org",
                                    "https://davisvanguard.org/sitemap_index.xml")
        a1 = await _insert(conn, src["id"], fx.ARTICLE_1_URL, fx.ARTICLE_1_TITLE, fx.ARTICLE_1_PUBLISHED, fx.ARTICLE_1_TEXT)

    stats = await orch.run()
    assert stats.failed == 0, stats.failures
    assert stats.processed == 1

    async with pool.connection() as conn:
        # 3. All 9 actors with correct types
        for name, etype in fx.ALL_9:
            row = await _entity_present(conn, name)
            assert row is not None, f"{name} not in TB_Entity"
            if not run_live or name != "Officer Pemberton":
                assert row["entity_type"] == etype, f"{name}: {row['entity_type']} != {etype}"

        cur = await conn.execute("SELECT event_id FROM TB_Article_Event WHERE article_id = %s", (a1,))
        events_a1 = {r["event_id"] for r in await cur.fetchall()}
        assert events_a1

        # 4. Follow-up article 5 days later
        a2 = await _insert(conn, src["id"], fx.ARTICLE_2_URL, fx.ARTICLE_2_TITLE, fx.ARTICLE_2_PUBLISHED, fx.ARTICLE_2_TEXT)

    stats2 = await orch.run()
    assert stats2.failed == 0, stats2.failures

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT event_id FROM TB_Article_Event WHERE article_id = %s", (a2,))
        events_a2 = {r["event_id"] for r in await cur.fetchall()}
        assert events_a2 & events_a1, f"follow-up created new event(s) {events_a2} instead of reusing {events_a1}"
        if not run_live:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM TB_Event")
            assert (await cur.fetchone())["n"] == 2  # Davis incident + Sacramento directive

        # Affiliation date bounds widened by the second article
        brackett = await _entity_present(conn, "Owen Brackett")
        affs = await repo.get_affiliations(conn, brackett["id"])
        if not run_live:
            cm = next(a for a in affs if a["role_title"] == "City Manager")
            assert cm["first_seen_date"] < cm["last_seen_date"]

        # 5. Dossier + coalition matrix
        marsh = await _entity_present(conn, "Helen Marsh")
        ledger = await repo.get_career_ledger(conn, marsh["id"])
        assert ledger
        topic_ledger = await repo.get_career_ledger(conn, marsh["id"], "immigration")
        if not run_live:
            assert topic_ledger

        cur = await conn.execute("SELECT * FROM VW_Entity_Coalition_Matrix WHERE entity_a_id = %s", (marsh["id"],))
        matrix = await cur.fetchall()
        assert matrix, "coalition matrix empty"
        if not run_live:
            ravel = await _entity_present(conn, "Gloria Ravel")
            pair = next(r for r in matrix if r["entity_b_id"] == ravel["id"])
            assert pair["alignment_index"] > 0  # both APOLOGETIC
            hale = await _entity_present(conn, "Robin Hale")
            opponents = await repo.get_coalition(conn, hale["id"], allies=False)
            assert any(r["entity_b_name"] == "Davis Police Department" for r in opponents)


def test_loose_quote_match_returns_article_wording():
    from civic_tracker.pipeline.stage2_llm import enforce_verbatim_quotes, locate_quote

    src = fx.ARTICLE_1_TEXT
    # exact (straight vs curly quote marks / apostrophes ignored)
    assert locate_quote("Let me be plain: the City of Davis does not cooperate with ICE enforcement", src).startswith(
        "Let me be plain: the City of Davis does not cooperate with ICE enforcement")
    # one word changed + coreference bracket inserted -> article's real wording
    got = locate_quote("I pulled over right away. He [Officer Pemberton] didn't explain anything. He kept me there and wrote a citation for honking", src)
    assert got == "I pulled over right away. He didn’t explain anything. He kept me there and wrote a ticket for honking"
    # dropped filler words -> snapped out to the full quoted span
    got = locate_quote("Nothing matters more to us than the trust residents place", src)
    assert got == "Nothing matters more to us than the trust residents place in this city"
    # paraphrase that isn't in the article -> None
    assert locate_quote("The mayor promised to fire the officer by Friday", src) is None

    from civic_tracker.pipeline.stage2_llm import ArticleExtractionResult
    res = ArticleExtractionResult.model_validate({"affiliations": [], "events": [{
        "canonical_title": "t", "event_type": "OTHER", "summary": "s", "topic_tags": ["x"],
        "involvements": [{"entity_name": "Robin Hale", "entity_type": "PERSON", "role_in_event": "r",
                          "stance": "OPPOSE", "action_summary": "a",
                          "direct_quotes": ["They talked for a minute while I waited ... The ICE truck pulled out and I followed it",
                                            "Totally invented sentence that never appears"]}]}]})
    res, dropped = enforce_verbatim_quotes(res, src)
    assert dropped == 1
    assert res.events[0].involvements[0].direct_quotes == [
        "They talked for a minute while I waited", "The ICE truck pulled out and I followed it"]


def test_coref_head_strips_possessive():
    text = "Hale’s car was stopped. She said it was wrong."
    out = annotate_coreferences(text, [[(0, 6), (24, 27)]])
    assert "She [Hale]" in out


def test_strip_honorific():
    from civic_tracker.pipeline.resolver import strip_honorific

    assert strip_honorific("Rep. Mike Tolliver") == "Mike Tolliver"
    assert strip_honorific("Vice Mayor Gloria Ravel") == "Gloria Ravel"
    assert strip_honorific("Former Mayor Helen Marsh") == "Helen Marsh"
    assert strip_honorific("Officer Pemberton") == "Officer Pemberton"
    assert strip_honorific("Davis Police Department") == "Davis Police Department"


def test_sanitize_drops_reasoning_leaks():
    from civic_tracker.pipeline.stage2_llm import ArticleExtractionResult, sanitize_result

    res = ArticleExtractionResult.model_validate({"affiliations": [
        {"person_name": "Helen Marsh", "organization_name": "City of Davis", "role_title": "Mayor"},
        {"person_name": "Helen Marsh", "organization_name": None,
         "role_title": "Mayor of Davis, Vice Mayor? No, just Mayor"},
    ], "events": []})
    res, n = sanitize_result(res)
    assert n == 1 and [a.role_title for a in res.affiliations] == ["Mayor"]


def test_newest_first_ordering():
    from civic_tracker.crawler.discovery import SitemapEntry, newest_first, parse_lastmod

    es = [SitemapEntry("https://x.org/2026/01/05/a/", None),
          SitemapEntry("https://x.org/about/", None),
          SitemapEntry("https://x.org/b/", parse_lastmod("2026-11-01T10:00:00+00:00")),
          SitemapEntry("https://x.org/2026/11/3/c", None)]
    assert [e.loc for e in newest_first(es)] == [
        "https://x.org/2026/11/3/c", "https://x.org/b/", "https://x.org/2026/01/05/a/", "https://x.org/about/"]


@ASYNC
async def test_polite_client_spacing_retry_after_and_give_up(monkeypatch):
    import time as _time

    import httpx

    from civic_tracker.crawler import polite

    sleeps = []
    real_sleep = polite.asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(round(s, 1))
        await real_sleep(0)

    monkeypatch.setattr(polite.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.url.path == "/throttle-once" and calls["n"] == 2:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if request.url.path == "/always-429":
            return httpx.Response(429)
        return httpx.Response(200, text="ok")

    async with polite.PoliteClient(delay=5, max_throttles=3, jitter_factor=0, pause_chance=0,
                                   transport=httpx.MockTransport(handler)) as c:
        t0 = _time.monotonic()
        assert (await c.get("https://x.org/a")).status_code == 200
        assert (await c.get("https://x.org/throttle-once")).status_code == 200  # 429 -> Retry-After 7 -> 200
        assert 7.0 in sleeps                       # honoured Retry-After
        assert any(4.0 <= s <= 5.0 for s in sleeps)  # spaced the 2nd request by ~delay
        with pytest.raises(polite.SourceThrottled):
            await c.get("https://x.org/always-429")
        assert c.throttle_events == 4
        assert _time.monotonic() - t0 < 5  # sleeps were faked


@ASYNC
async def test_rss_discovery_filters_window():
    from datetime import date

    import httpx

    from civic_tracker.crawler.discovery import discover_rss_urls

    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><link>https://x.org/news/a.html</link><pubDate>Fri, 06 Nov 2026 10:00:00 -0800</pubDate></item>
      <item><link>https://x.org/news/b.html</link><pubDate>Mon, 02 Nov 2026 10:00:00 -0800</pubDate></item>
      <item><link>https://x.org/news/old.html</link><pubDate>Mon, 05 Oct 2026 10:00:00 -0700</pubDate></item>
    </channel></rss>"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=rss))) as c:
        urls = await discover_rss_urls(c, "https://x.org/feed", since=date(2026, 11, 1))
    assert urls == ["https://x.org/news/a.html", "https://x.org/news/b.html"]


@ASYNC
async def test_claim_newest_first_release_and_stage3_eligibility(pool):
    """Queue semantics + Stage 3 threshold, against the throwaway schema."""
    from datetime import datetime, timezone

    from civic_tracker.pipeline.stage3_wiki import build_context, eligible_dirty_entities

    async with pool.connection() as conn:
        src = await repo.add_source(conn, "Queue Test", "https://queue.example")
        ids = {}
        for day in (1, 3, 2):
            ids[day] = await repo.insert_article(
                conn, source_id=src["id"], url=f"https://queue.example/{day}", title=f"day {day}", author=None,
                published_at=datetime(2030, 1, day, tzinfo=timezone.utc), full_text=f"text {day} " * 50,
                content_hash=f"queue-hash-{day}")
        first = await repo.claim_next_article(conn)
        second = await repo.claim_next_article(conn)
        assert (first["id"], second["id"]) == (ids[3], ids[2])  # newest published first
        assert first["prev_status"] == "PENDING"
        await repo.release_article(conn, first["id"])
        again = await repo.claim_next_article(conn)
        assert again["id"] == ids[3]
        for aid in ids.values():
            await repo.set_article_status(conn, aid, "SKIPPED")

        # From the e2e test above: Davis actors have 1-2 events; none reach the 3-event bar,
        # but anyone with 2 events + an affiliation qualifies.
        eligible = await eligible_dirty_entities(conn, 100)
        cur = await conn.execute("""
            SELECT e.id FROM TB_Entity e
            WHERE (SELECT COUNT(DISTINCT event_id) FROM TB_Entity_Event WHERE entity_id = e.id) >= 3
               OR ((SELECT COUNT(DISTINCT event_id) FROM TB_Entity_Event WHERE entity_id = e.id) >= 2
                   AND EXISTS (SELECT 1 FROM TB_Entity_Affiliation WHERE entity_id = e.id))""")
        assert sorted(eligible) == sorted(r["id"] for r in await cur.fetchall())
        if eligible:
            ctx = await build_context(conn, eligible[0])
            assert "Most salient events" in ctx and "Salience:" in ctx


@ASYNC
async def test_worker_processes_queue_and_commands(pool):
    import asyncio as _asyncio
    from datetime import datetime, timezone

    from civic_tracker.worker import Worker

    class FakeBioWriter:
        async def write(self, context, on_call=None):
            return "[[Davis Police Department]] is a test bio."

    async with pool.connection() as conn:
        await conn.execute("UPDATE TB_Scheduler_State SET is_enabled = FALSE, processor_enabled = TRUE WHERE id = 1")
        src = await repo.add_source(conn, "Worker Test", "https://worker.example")
        aid = await repo.insert_article(
            conn, source_id=src["id"], url="https://worker.example/a", title=fx.ARTICLE_1_TITLE, author=None,
            published_at=datetime(2031, 1, 1, tzinfo=timezone.utc), full_text=fx.ARTICLE_1_TEXT,
            content_hash="worker-hash-1")

    stage1, extractor, encoder = _components(False)
    w = Worker(pool, slots=2)
    w.orch.stage1, w.orch.extractor, w.orch.encoder, w.encoder = stage1, extractor, encoder, encoder
    w.bio_writer = FakeBioWriter()
    task = _asyncio.create_task(w.run())

    async def wait_for(sql, params, want):
        for _ in range(450):  # up to 90 s: the shared remote DB can be slow while the live worker runs
            await _asyncio.sleep(0.2)
            async with pool.connection() as conn:
                got = (await (await conn.execute(sql, params)).fetchone())["v"]
            if got == want:
                return got
        return got

    try:
        st = await wait_for("SELECT processing_status AS v FROM TB_Article WHERE id=%s", (aid,), "PROCESSED")
        assert st == "PROCESSED", st
        # article done (it created / re-dirtied the police dept); now regenerate that bio via the command queue
        async with pool.connection() as conn:
            ent = await repo.find_entity_by_name(conn, "Davis Police Department")
            await conn.execute("INSERT INTO TB_Worker_Command (command, params) VALUES ('REGENERATE_BIO', %s)",
                               (f'{{"entity_id": {ent["id"]}}}',))
        cmd = await wait_for("SELECT status AS v FROM TB_Worker_Command ORDER BY id DESC LIMIT 1", None, "DONE")
        assert cmd == "DONE", cmd
        async with pool.connection() as conn:
            hb = await (await conn.execute("SELECT * FROM TB_Worker_Status WHERE id='main'")).fetchone()
            bio = await (await conn.execute("SELECT summary_bio, bio_is_dirty FROM TB_Entity WHERE id=%s", (ent["id"],))).fetchone()
        assert hb["heartbeat_at"] is not None and len(hb["slots"]) == 2
        assert bio["summary_bio"].startswith("[[Davis Police Department]]") and bio["bio_is_dirty"] is False
    finally:
        w.stop_event.set()
        await _asyncio.wait_for(task, 30)
    async with pool.connection() as conn:
        runs = await (await conn.execute("SELECT job_type, status FROM TB_Job_Run ORDER BY id")).fetchall()
        hb = await (await conn.execute("SELECT processor_state FROM TB_Worker_Status WHERE id='main'")).fetchone()
        claimed = await (await conn.execute("SELECT COUNT(*) AS n FROM TB_Article WHERE processing_status='IN_PROGRESS'")).fetchone()
    assert ("PROCESS_PIPELINE", "SUCCESS") in [(r["job_type"], r["status"]) for r in runs]
    assert hb["processor_state"] == "STOPPED" and claimed["n"] == 0


@ASYNC
async def test_worker_interleaves_bios_while_queue_busy(pool, monkeypatch):
    import asyncio as _asyncio
    from datetime import datetime, timezone

    from civic_tracker import worker as worker_mod
    from civic_tracker.config import settings as _settings

    written = []

    class SlowExtractor:  # keeps the queue busy long enough for the interleave to trigger
        async def extract(self, *a, **k):
            await _asyncio.sleep(0.5)
            return fx.MOCK_RESULT_2

    class FakeBioWriter:
        async def write(self, context, on_call=None):
            written.append(context.splitlines()[0])
            return "Interleaved test bio."

    async with pool.connection() as conn:
        await conn.execute("UPDATE TB_Scheduler_State SET is_enabled = FALSE, processor_enabled = TRUE WHERE id = 1")
        src = await repo.add_source(conn, "Interleave Test", "https://interleave.example")
        for i in range(6):
            await repo.insert_article(
                conn, source_id=src["id"], url=f"https://interleave.example/{i}", title=f"Interleave {i}", author=None,
                published_at=datetime(2032, 1, 1 + i, tzinfo=timezone.utc), full_text=fx.ARTICLE_2_TEXT + f" ({i})",
                content_hash=f"interleave-{i}")
        ent = await repo.find_entity_by_name(conn, "Robin Hale")
        await conn.execute("UPDATE TB_Entity SET bio_is_dirty = TRUE, summary_bio = NULL WHERE id = %s", (ent["id"],))

    async def only_hale(conn, limit=1):
        return [ent["id"]]

    monkeypatch.setattr(_settings, "BIO_INTERLEAVE_MINUTES", 0.005)  # 0.3 s
    monkeypatch.setattr(worker_mod, "eligible_dirty_entities", only_hale)
    stage1, _, encoder = _components(False)
    w = worker_mod.Worker(pool, slots=1)
    w.orch.stage1, w.orch.extractor, w.orch.encoder, w.encoder = stage1, SlowExtractor(), encoder, encoder
    w.bio_writer = FakeBioWriter()
    w.last_bio_at -= 10  # due immediately
    task = _asyncio.create_task(w.run())
    try:
        for _ in range(100):
            await _asyncio.sleep(0.2)
            if written:
                break
        async with pool.connection() as conn:
            queued = (await (await conn.execute(
                "SELECT COUNT(*) AS n FROM TB_Article WHERE source_id=%s AND processing_status IN ('PENDING','STAGE1_DONE')",
                (src["id"],))).fetchone())["n"]
        assert written, "no interleaved bio was written"
        assert queued > 0, "bio should be written while articles are still queued"
    finally:
        w.stop_event.set()
        await _asyncio.wait_for(task, 30)


@ASYNC
async def test_llm_telemetry_recorded(pool):
    """Stage 2 attempts (OK and ERROR) land in TB_LLM_Call with duration, usage and concurrency."""
    import asyncio as _asyncio
    import types

    from civic_tracker.pipeline.stage2_llm import DeepSeekExtractor
    from civic_tracker.pipeline.telemetry import record_llm_call

    calls = {"n": 0}

    class FakeCompletions:
        async def create(self, **kw):
            calls["n"] += 1
            await _asyncio.sleep(0.05)
            if calls["n"] == 1:
                raise ValueError("server hiccup")  # retryable (parse-type) error
            msg = types.SimpleNamespace(content=fx.MOCK_RESULT_2.model_dump_json())
            usage = types.SimpleNamespace(prompt_tokens=1200, completion_tokens=900,
                                          completion_tokens_details=types.SimpleNamespace(reasoning_tokens=700))
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)

    ex = DeepSeekExtractor()
    ex.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))

    async def on_call(timer, meta, status, error):
        await record_llm_call(pool, kind="STAGE2_EXTRACT", timer=timer, meta=meta, status=status, error=error,
                              input_chars=1234)

    from tenacity import wait_none
    ex._call.retry.wait = wait_none()
    res = await ex.extract("t", None, [], "bundle", on_call=on_call)
    assert res.events
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            "SELECT status, duration_ms, prompt_tokens, completion_tokens, reasoning_tokens, concurrent_calls, error "
            "FROM TB_LLM_Call ORDER BY id")).fetchall()
    assert [r["status"] for r in rows][-2:] == ["ERROR", "OK"], rows
    ok = rows[-1]
    assert ok["duration_ms"] >= 40 and ok["completion_tokens"] == 900 and ok["reasoning_tokens"] == 700
    assert ok["concurrent_calls"] >= 1 and "hiccup" in rows[-2]["error"]


@ASYNC
async def test_history_walk_wp_api(pool, monkeypatch):
    from datetime import date, datetime, timezone

    import httpx

    from civic_tracker.crawler import polite, scraper

    long_text = "<p>" + ("City council discussed the budget at length. " * 20) + "</p>"
    posts = [  # newest first, like wp-json
        {"link": f"https://hist.example/{d}", "date_gmt": f"2026-09-{d:02d}T12:00:00",
         "title": {"rendered": f"Older story {d}"}, "content": {"rendered": long_text + f"<p>{d}</p>"}}
        for d in (28, 20, 10)
    ]
    seen = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        seen.append(dict(req.url.params))
        before = req.url.params.get("before", "9999")
        page = [p for p in posts if p["date_gmt"] < before] if req.url.params.get("page") == "1" else []
        return httpx.Response(200, json=page, headers={"X-WP-TotalPages": "1"})

    monkeypatch.setattr(scraper, "make_client", lambda delay=0: polite.PoliteClient(
        delay=0, transport=httpx.MockTransport(handler)))
    async with pool.connection() as conn:
        src = await repo.add_source(conn, "History Test", "https://hist.example",
                                    scrape_config={"method": "wp_api"})
        await repo.insert_article(conn, source_id=src["id"], url="https://hist.example/new", title="New", author=None,
                                  published_at=datetime(2026, 10, 1, tzinfo=timezone.utc), full_text="x " * 400,
                                  content_hash="hist-new")
    st = await scraper.crawl_source_history(pool, src, floor=date(2015, 1, 1), batch=2)
    assert st.inserted == 2, st  # batch=2 -> the two newest older posts
    assert seen[-1]["before"].startswith("2026-10-01") and seen[-1]["after"].startswith("2015-01-01")
    async with pool.connection() as conn:
        urls = {r["url"] for r in await (await conn.execute(
            "SELECT url FROM TB_Article WHERE source_id=%s", (src["id"],))).fetchall()}
    assert {"https://hist.example/28", "https://hist.example/20"} <= urls
    st = await scraper.crawl_source_history(pool, src, floor=date(2015, 1, 1), batch=2)
    assert st.inserted == 1  # the 10th
    async with pool.connection() as conn:
        src = await (await conn.execute("SELECT * FROM TB_Sources WHERE id=%s", (src["id"],))).fetchone()
    st = await scraper.crawl_source_history(pool, src, floor=date(2015, 1, 1), batch=2)
    assert st is not None and st.inserted == 0
    async with pool.connection() as conn:
        cfg = (await (await conn.execute("SELECT scrape_config FROM TB_Sources WHERE id=%s", (src["id"],))).fetchone())["scrape_config"]
    assert cfg.get("history_done") is True, cfg
    assert await scraper.crawl_source_history(pool, dict(src, scrape_config=cfg), floor=date(2015, 1, 1), batch=2) is None


@ASYNC
async def test_history_walk_starts_empty_source_and_feed_uses_sitemap(pool, monkeypatch):
    """A source with nothing stored starts walking back from today; an RSS-only source walks via its sitemap."""
    from datetime import date

    import httpx

    from civic_tracker.crawler import polite, scraper

    body = "<p>" + ("The Woodland city council voted on the downtown plan this week. " * 20) + "</p>"
    sitemap = ("<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
               "<url><loc>https://feedsite.example/news/2024/05/03/plan.html</loc></url>"
               "<url><loc>https://feedsite.example/news/2023/02/01/budget.html</loc></url></urlset>")
    paths = []

    def handler(req: httpx.Request):
        paths.append(req.url.path)
        if req.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        if req.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap, headers={"content-type": "application/xml"})
        if req.url.path.startswith("/news/"):
            y, m, d = req.url.path.split("/")[2:5]
            return httpx.Response(200, html=f"<html><head><title>Story {y}</title>"
                                  f"<meta property='article:published_time' content='{y}-{m}-{d}T10:00:00-07:00'>"
                                  f"</head><body><article><h1>Story {y}</h1>{body}</article></body></html>")
        return httpx.Response(404)

    monkeypatch.setattr(scraper, "make_client", lambda delay=0: polite.PoliteClient(
        delay=0, transport=httpx.MockTransport(handler)))
    assert scraper.history_method({"method": "feed"}) == "html"
    assert scraper.history_method({"method": "feed", "history_method": "none"}) is None
    async with pool.connection() as conn:
        src = await repo.add_source(conn, "Feed Only", "https://feedsite.example",
                                    scrape_config={"method": "feed"})
    st = await scraper.crawl_source_history(pool, src, floor=date(2015, 1, 1), batch=5)
    assert st is not None and st.inserted == 2, (st, paths)  # no stored articles, RSS-only: still walked back
    assert "/sitemap.xml" in paths


@ASYNC
async def test_history_walk_feed_dated(pool, monkeypatch):
    """BLOX-style search feed: history asks for items before the cursor via d1/d2; a site that ignores
    the date params (returns only recent items) ends the walk instead of looping on recent pages."""
    from datetime import date, datetime, timezone

    import httpx

    from civic_tracker.crawler import polite, scraper

    body = "<p>" + ("The Davis city council approved the downtown parking plan on Tuesday. " * 20) + "</p>"
    old = [("2019-03-0%d" % d, f"https://blox.example/news/local/story-{d}.html") for d in (5, 4, 3)]
    honour = {"on": True}
    seen = []

    def rss(items):
        xs = "".join(f"<item><link>{u}</link><pubDate>{date.fromisoformat(d).strftime('%a, %d %b %Y')} 10:00:00 -0800"
                     f"</pubDate><title>t</title></item>" for d, u in items)
        return f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{xs}</channel></rss>"

    def handler(req: httpx.Request):
        if req.url.path.endswith("robots.txt"):
            return httpx.Response(404)
        if req.url.path == "/search/":
            seen.append(dict(req.url.params))
            d2 = req.url.params.get("d2")
            if honour["on"] and d2:
                return httpx.Response(200, text=rss([i for i in old if i[0] < d2]))
            return httpx.Response(200, text=rss([("2026-11-07", "https://blox.example/news/local/new.html")]))
        d = req.url.path.split("-")[-1].split(".")[0]
        return httpx.Response(200, html=f"<html><head><title>Story {d}</title><meta property='article:published_time' "
                              f"content='2019-03-0{d}T10:00:00-08:00'></head><body><article>{body}<p>Story number {d}.</p></article></body></html>")

    monkeypatch.setattr(scraper, "make_client", lambda delay=0: polite.PoliteClient(
        delay=0, transport=httpx.MockTransport(handler)))
    cfg = {"method": "feed", "history_method": "feed_dated"}
    async with pool.connection() as conn:
        src = await repo.add_source(conn, "Blox Paper", "https://blox.example",
                                    rss_url="https://blox.example/search/?f=rss&t=article&c=news*",
                                    scrape_config=cfg)
    st = await scraper.crawl_source_history(pool, src, floor=date(2015, 1, 1), batch=2)
    assert st.inserted == 2, (st, seen)
    assert seen[-1]["d1"] == "2015-01-01" and seen[-1]["c"] == "news*"
    async with pool.connection() as conn:
        src = await (await conn.execute("SELECT * FROM TB_Sources WHERE id=%s", (src["id"],))).fetchone()
    st = await scraper.crawl_source_history(pool, src, floor=date(2015, 1, 1), batch=2)
    assert st.inserted == 1 and seen[-1]["d2"] == "2019-03-04"  # cursor moved back to the oldest stored

    honour["on"] = False  # site ignores d1/d2 -> nothing in window -> walk ends
    async with pool.connection() as conn:
        src2 = await repo.add_source(conn, "Blox Ignores Dates", "https://blox2.example",
                                     rss_url="https://blox2.example/search/?f=rss", scrape_config=cfg)
        await repo.insert_article(conn, source_id=src2["id"], url="https://blox2.example/news/local/seed.html",
                                  title="Seed", author=None, published_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                                  full_text="x " * 400, content_hash="blox2-seed")
    st = await scraper.crawl_source_history(pool, src2, floor=date(2015, 1, 1), batch=2)
    assert st.inserted == 0 and st.discovered == 0
    async with pool.connection() as conn:
        cfg2 = (await (await conn.execute("SELECT scrape_config FROM TB_Sources WHERE id=%s", (src2["id"],))).fetchone())["scrape_config"]
    assert cfg2.get("history_done") is True


@ASYNC
async def test_api_burst_newest_first_limit_and_cap(pool, monkeypatch):
    import asyncio as _asyncio
    import types
    from datetime import datetime, timezone

    from civic_tracker import worker as worker_mod
    from civic_tracker.config import settings as _settings
    from civic_tracker.pipeline.stage2_llm import DeepSeekExtractor

    requests = []

    class FakeCompletions:
        async def create(self, **kw):
            requests.append(kw)
            await _asyncio.sleep(0.02)
            msg = types.SimpleNamespace(content=fx.MOCK_RESULT_2.model_dump_json())
            usage = types.SimpleNamespace(prompt_tokens=4000, completion_tokens=5000,
                                          completion_tokens_details=types.SimpleNamespace(reasoning_tokens=3000))
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)

    def fake_burst():
        ex = DeepSeekExtractor(base_url="http://api.invalid/v1", api_key="k", model="deepseek-flash",
                               provider="deepseek-api", response_format="json_object", extra_body={},
                               price_in_per_m=0.30, price_out_per_m=1.20)
        ex.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
        return ex

    monkeypatch.setattr(DeepSeekExtractor, "burst", staticmethod(fake_burst))
    monkeypatch.setattr(_settings, "BURST_LLM_API_KEY", "test-key")
    async with pool.connection() as conn:
        # isolate: no crawl, local processor paused, empty queue apart from ours
        await conn.execute("UPDATE TB_Scheduler_State SET is_enabled = FALSE, processor_enabled = FALSE WHERE id = 1")
        await conn.execute("UPDATE TB_Article SET processing_status = 'SKIPPED' WHERE processing_status IN ('PENDING','STAGE1_DONE')")
        src = await repo.add_source(conn, "Burst Test", "https://burst.example")
        ids = []
        for d in range(1, 6):
            ids.append(await repo.insert_article(
                conn, source_id=src["id"], url=f"https://burst.example/{d}", title=f"Burst {d}", author=None,
                published_at=datetime(2020, 1, d, tzinfo=timezone.utc), full_text=fx.ARTICLE_2_TEXT + f" [{d}]",
                content_hash=f"burst-{d}"))
        await conn.execute("INSERT INTO TB_Worker_Command (command, params) VALUES ('BURST', %s)",
                           ('{"limit": 3, "max_spend_usd": 10, "concurrency": 2}',))

    stage1, _, encoder = _components(False)
    w = worker_mod.Worker(pool, slots=1)
    w.orch.stage1, w.orch.encoder, w.encoder = stage1, encoder, encoder
    task = _asyncio.create_task(w.run())

    async def wait_cmd(want_n):
        for _ in range(450):  # up to 90 s: the shared remote DB can be slow while the live worker runs
            await _asyncio.sleep(0.2)
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT status, result FROM TB_Worker_Command WHERE command='BURST' ORDER BY id")).fetchall()
            if len(rows) >= want_n and rows[want_n - 1]["status"] in ("DONE", "FAILED"):
                return rows[want_n - 1]
        return rows[-1]

    try:
        r1 = await wait_cmd(1)
        assert r1["status"] == "DONE" and "article limit 3" in r1["result"], r1
        async with pool.connection() as conn:
            done = {r["id"] for r in await (await conn.execute(
                "SELECT id FROM TB_Article WHERE source_id=%s AND processing_status='PROCESSED'", (src["id"],))).fetchall()}
            calls = await (await conn.execute(
                "SELECT provider, cost_usd FROM TB_LLM_Call WHERE provider='deepseek-api'")).fetchall()
            # cap: each fake call costs 4000*0.30/1e6 + 5000*1.20/1e6 = $0.0072 -> cap $0.001 stops after 1st call
            await conn.execute("INSERT INTO TB_Worker_Command (command, params) VALUES ('BURST', %s)",
                               ('{"limit": 10, "max_spend_usd": 0.001, "concurrency": 1}',))
        assert done == set(ids[2:]), (done, ids)            # the three NEWEST (same order as local)
        assert len(calls) == 3 and float(calls[0]["cost_usd"]) == pytest.approx(0.0072)
        assert requests[0]["response_format"] == {"type": "json_object"}
        assert "JSON Schema" in requests[0]["messages"][0]["content"]
        r2 = await wait_cmd(2)
        assert "spend cap" in r2["result"], r2
        async with pool.connection() as conn:
            left = (await (await conn.execute(
                "SELECT COUNT(*) AS n FROM TB_Article WHERE source_id=%s AND processing_status IN ('PENDING','STAGE1_DONE')",
                (src["id"],))).fetchone())["n"]
            run = await (await conn.execute("SELECT status FROM TB_Job_Run WHERE job_type='API_BURST' ORDER BY id DESC LIMIT 1")).fetchone()
        assert left == 1 and run["status"] == "SUCCESS"
    finally:
        w.stop_event.set()
        await _asyncio.wait_for(task, 30)
        async with pool.connection() as conn:
            await conn.execute("UPDATE TB_Scheduler_State SET processor_enabled = TRUE WHERE id = 1")


@ASYNC
async def test_local_and_burst_never_claim_the_same_article(pool):
    """Local slots and burst slots race over one queue, both newest-first: every article must be
    extracted exactly once, and nothing may be left IN_PROGRESS."""
    import asyncio as _asyncio
    import random
    from datetime import datetime, timezone

    from civic_tracker.pipeline.orchestrator import Orchestrator, RunStats

    seen: dict = {}

    class CountingExtractor:
        def __init__(self, name):
            self.name = name

        async def extract(self, title, published_at, candidate_entities, evidence_bundle, on_call=None, genre=None,
                      jurisdiction=None, **_kw):
            seen.setdefault(title, []).append(self.name)
            await _asyncio.sleep(random.uniform(0.01, 0.08))
            return fx.MOCK_RESULT_2

    async with pool.connection() as conn:
        await conn.execute("UPDATE TB_Article SET processing_status = 'SKIPPED' "
                           "WHERE processing_status IN ('PENDING','STAGE1_DONE')")
        src = await repo.add_source(conn, "Race Test", "https://race.example")
        for i in range(24):
            await repo.insert_article(
                conn, source_id=src["id"], url=f"https://race.example/{i}", title=f"Race {i}", author=None,
                published_at=datetime(2033, 1, 1 + i % 28, i % 24, tzinfo=timezone.utc),
                full_text=fx.ARTICLE_2_TEXT + f" ({i})", content_hash=f"race-{i}")

    stage1, _, encoder = _components(False)
    local = Orchestrator(pool, stage1=stage1, extractor=CountingExtractor("local"), encoder=encoder, concurrency=3)
    burst = Orchestrator(pool, stage1=stage1, extractor=CountingExtractor("burst"), encoder=encoder, concurrency=5)
    s_local, s_burst = RunStats(), RunStats()
    await _asyncio.gather(*[local.slot_loop(i, s_local) for i in range(3)],
                          *[burst.slot_loop(i, s_burst) for i in range(5)])

    assert len(seen) == 24, len(seen)
    doubles = {t: who for t, who in seen.items() if len(who) != 1}
    assert not doubles, doubles
    assert {"local", "burst"} <= {w for who in seen.values() for w in who}  # both actually worked
    async with pool.connection() as conn:
        left = await (await conn.execute(
            "SELECT processing_status, COUNT(*) AS n FROM TB_Article WHERE source_id=%s GROUP BY 1", (src["id"],))).fetchall()
    assert [(r["processing_status"], r["n"]) for r in left] == [("PROCESSED", 24)], left


class _FakeClassifier:
    """Stands in for the LLM: labels by keywords in the title."""

    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    async def classify(self, title, lead, source_name):
        from civic_tracker.pipeline.triage import TriageLabel

        self.calls += 1
        if self.fail:
            raise RuntimeError("LLM down")
        if "Monterey" in title:
            return TriageLabel(genre="news", yolo_relevance="none", reason="Monterey County story")
        if "game" in title:
            return TriageLabel(genre="sports", yolo_relevance="local", reason="game recap")
        return TriageLabel(genre="news", yolo_relevance="local", reason="Davis city council")


@ASYNC
async def test_triage_rules():
    from civic_tracker.pipeline.triage import Triage, locality_score, url_allowed

    src = {"name": "Winters Express", "scrape_config": {"url_exclude": [r"/features/"]}}
    t = Triage(_FakeClassifier())
    chk = lambda url, title: t.check(url=url, title=title, text="Davis council voted.", source=src)  # noqa: E731
    r = await chk("https://w.example/online_features/press_releases/treadmill", "KingSmith launches treadmill")
    assert not r.keep and "online_features" in r.reason
    r = await chk("https://w.example/features/health_wellness/x", "Aging tips")
    assert not r.keep and "/features/" in r.reason                       # per-source exclude
    r = await chk("https://w.example/news/local/x", "15 warm fall casserole dishes for you")
    assert not r.keep and r.reason == "non-news title"
    r = await chk("https://w.example/news/local/x", "Monterey County orders ICE off county property")
    assert not r.keep and r.reason.startswith("not local") and r.relevance == "none"
    r = await chk("https://w.example/news/local/x", "Blue Devils win game")
    assert not r.keep and r.reason.startswith("not news (sports)")
    r = await chk("https://w.example/news/local/x", "Council adopts budget")
    assert r.keep and r.genre == "news" and r.relevance == "local"
    # press releases are wanted (only the syndicated /online_features/ section is excluded)
    assert url_allowed("https://yoloda.org/media/press-releases/da-announces-x/", {})
    assert url_allowed("https://city.example/press_releases/council-update", {})
    assert not url_allowed("https://wintersexpress.com/online_features/press_releases/x", {})
    assert url_allowed("https://x.example/news/local/a", {"url_include": [r"/news/local/"]})
    assert not url_allowed("https://x.example/news/state/a", {"url_include": [r"/news/local/"]})
    # fail open
    r = await Triage(_FakeClassifier(fail=True)).check(url="https://x/a", title="Council", text="", source=src)
    assert r.keep and "classifier error" in r.reason
    # locality score ignores the source's own name in bylines
    assert locality_score("Story", "Davis Vanguard reports from Los Angeles") == 0
    assert locality_score("Woodland council", "Yolo County supervisors met in Woodland") >= 4


@ASYNC
async def test_orchestrator_triage_skips_and_override(pool):
    from datetime import datetime, timezone

    from civic_tracker.pipeline.orchestrator import Orchestrator
    from civic_tracker.pipeline.triage import Triage

    async with pool.connection() as conn:
        await conn.execute("UPDATE TB_Article SET processing_status = 'SKIPPED' "
                           "WHERE processing_status IN ('PENDING','STAGE1_DONE')")
        src = await repo.add_source(conn, "Triage Test", "https://triage.example")
        ids = {}
        for i, title in enumerate(["Council adopts budget", "Monterey County orders ICE off property",
                                   "Blue Devils win game"]):
            ids[title] = await repo.insert_article(
                conn, source_id=src["id"], url=f"https://triage.example/news/{i}", title=title, author=None,
                published_at=datetime(2034, 1, 1 + i, tzinfo=timezone.utc), full_text=fx.ARTICLE_2_TEXT + f" {i}",
                content_hash=f"triage-{i}")

    stage1, extractor, encoder = _components(False)
    clf = _FakeClassifier()
    orch = Orchestrator(pool, stage1=stage1, extractor=extractor, encoder=encoder, concurrency=2,
                        triage=Triage(clf))
    stats = await orch.run()
    async with pool.connection() as conn:
        rows = {r["title"]: r for r in await (await conn.execute(
            "SELECT title, processing_status, skip_reason, genre, yolo_relevance, triaged_at FROM TB_Article "
            "WHERE source_id=%s", (src["id"],))).fetchall()}
    assert rows["Council adopts budget"]["processing_status"] == "PROCESSED"
    assert rows["Council adopts budget"]["genre"] == "news" and rows["Council adopts budget"]["triaged_at"]
    assert rows["Monterey County orders ICE off property"]["processing_status"] == "SKIPPED"
    assert "not local" in rows["Monterey County orders ICE off property"]["skip_reason"]
    assert rows["Blue Devils win game"]["skip_reason"].startswith("not news (sports)")
    assert stats.skipped == 2 and stats.processed == 1

    # admin "process anyway": skip_reason='override' bypasses triage once
    async with pool.connection() as conn:
        await conn.execute("UPDATE TB_Article SET processing_status='PENDING', skip_reason='override' WHERE id=%s",
                           (ids["Blue Devils win game"],))
    calls_before = clf.calls
    await orch.run()
    async with pool.connection() as conn:
        st = (await (await conn.execute("SELECT processing_status FROM TB_Article WHERE id=%s",
                                        (ids["Blue Devils win game"],))).fetchone())["processing_status"]
    assert st == "PROCESSED" and clf.calls == calls_before


def _bias_result(claimed: bool):
    """One event where Candidate X and President Y both 'SUPPORT' - the shape of the op-ed that tied
    a candidate to Trump. claimed=True marks both as third-party claims (the new opinion extraction)."""
    from civic_tracker.pipeline.stage2_llm import ArticleExtractionResult

    ev = "claimed" if claimed else "reported"
    return ArticleExtractionResult.model_validate({"affiliations": [], "events": [{
        "canonical_title": "Bias test foreign policy debate", "event_type": "PUBLIC_CONTROVERSY",
        "event_date_iso": "2035-03-01", "summary": "A debate about strikes abroad.", "topic_tags": ["foreign-policy"],
        "involvements": [
            {"entity_name": "Xavier Quill", "entity_type": "PERSON", "role_in_event": "candidate",
             "stance": "SUPPORT", "action_summary": "Supported strikes.", "evidence": ev,
             "claimed_by": "Op Ed Writer" if claimed else None},
            {"entity_name": "President Yorick Zane", "entity_type": "PERSON", "role_in_event": "president",
             "stance": "SUPPORT", "action_summary": "Ordered strikes.", "evidence": ev,
             "claimed_by": "Op Ed Writer" if claimed else None}]}]})


@ASYNC
async def test_weighting_blocks_opinion_alliances_and_reextract_replaces(pool):
    from datetime import datetime, timezone

    from civic_tracker.pipeline.orchestrator import Orchestrator, RunStats

    async with pool.connection() as conn:
        await conn.execute("UPDATE TB_Article SET processing_status = 'SKIPPED' "
                           "WHERE processing_status IN ('PENDING','STAGE1_DONE')")
        blog = await repo.add_source(conn, "Bias Blog", "https://biasblog.example")
        paper = await repo.add_source(conn, "Bias Paper", "https://biaspaper.example")
        await conn.execute("UPDATE TB_Sources SET reliability = 0.3 WHERE id = %s", (blog["id"],))
        op = await repo.insert_article(
            conn, source_id=blog["id"], url="https://biasblog.example/op", title="Candidate's chilling position",
            author=None, published_at=datetime(2035, 3, 2, tzinfo=timezone.utc), full_text=fx.ARTICLE_2_TEXT + " op",
            content_hash="bias-op")
        await conn.execute("UPDATE TB_Article SET genre = 'opinion', triaged_at = NOW() WHERE id = %s", (op,))

    class FixedExtractor:
        def __init__(self, result):
            self.result = result

        async def extract(self, *a, **k):
            return self.result

    stage1, _, encoder = _components(False)
    # 1) old-style extraction of the op-ed: both rows "reported" -> only reliability 0.3 x opinion 0.35 = 0.105
    await Orchestrator(pool, stage1=stage1, extractor=FixedExtractor(_bias_result(False)), encoder=encoder).run()

    async def pair_alignment():
        async with pool.connection() as conn:
            return await (await conn.execute(
                """SELECT m.alignment_index FROM VW_Entity_Coalition_Matrix m
                   JOIN TB_Entity a ON a.id = m.entity_a_id AND a.canonical_name = 'Xavier Quill'
                   JOIN TB_Entity b ON b.id = m.entity_b_id AND b.canonical_name = 'Yorick Zane'""")).fetchone()

    assert await pair_alignment() is None, "a low-reliability opinion piece must not create an alliance"
    async with pool.connection() as conn:
        xq = (await (await conn.execute("SELECT id FROM TB_Entity WHERE canonical_name='Xavier Quill'")).fetchone())["id"]
        ev = await (await conn.execute(
            "SELECT provisional FROM TB_Event WHERE canonical_title='Bias test foreign policy debate'")).fetchone()
        sal = await (await conn.execute(
            "SELECT commentary_only FROM VW_Entity_Salient_Career_Events WHERE entity_id=%s", (xq,))).fetchone()
    assert ev["provisional"] is True and sal["commentary_only"] is True

    # 2) dispute one row, then re-extract with the new (claimed) shape: rows replaced, dispute kept
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT id FROM TB_Entity_Event WHERE entity_id=%s AND source_article_id=%s", (xq, op))).fetchone()
        await conn.execute("UPDATE TB_Entity_Event SET disputed = TRUE, dispute_note='test' WHERE id=%s", (row["id"],))
    stats = RunStats()
    await Orchestrator(pool, stage1=stage1, extractor=FixedExtractor(_bias_result(True)), encoder=encoder,
                       concurrency=1).reextract([op], stats)
    assert stats.processed == 1, stats.log_lines
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            """SELECT e.canonical_name, ee.evidence_type, ee.claimed_by, ee.disputed FROM TB_Entity_Event ee
               JOIN TB_Entity e ON e.id = ee.entity_id WHERE ee.source_article_id=%s ORDER BY 1""", (op,))).fetchall()
    by = {r["canonical_name"]: r for r in rows}
    assert by["Xavier Quill"]["disputed"] is True                      # admin decision survived
    assert by["Yorick Zane"]["evidence_type"] == "claimed" and by["Yorick Zane"]["claimed_by"] == "Op Ed Writer"

    # 3) the same facts from a reliable NEWS report do form an alliance, and confirm the event
    async with pool.connection() as conn:
        news = await repo.insert_article(
            conn, source_id=paper["id"], url="https://biaspaper.example/n", title="Debate report", author=None,
            published_at=datetime(2035, 3, 2, tzinfo=timezone.utc), full_text=fx.ARTICLE_2_TEXT + " news",
            content_hash="bias-news")
        await conn.execute("UPDATE TB_Article SET genre = 'news', triaged_at = NOW() WHERE id = %s", (news,))
        await conn.execute("UPDATE TB_Entity_Event SET disputed = FALSE WHERE entity_id=%s", (xq,))
    await Orchestrator(pool, stage1=stage1, extractor=FixedExtractor(_bias_result(False)), encoder=encoder).run()
    al = await pair_alignment()
    assert al is not None and float(al["alignment_index"]) > 0
    async with pool.connection() as conn:
        ev = await (await conn.execute(
            "SELECT provisional FROM TB_Event WHERE canonical_title='Bias test foreign policy debate'")).fetchone()
    assert ev["provisional"] is False
