"""civic-tracker command line interface."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from civic_tracker.db import repositories as repo
from civic_tracker.db.connection import close_pool, get_pool, init_schema

app = typer.Typer(help="Civic entity, event, and coalition intelligence pipeline.", no_args_is_help=True)
console = Console()


def _run(coro):
    async def wrapper():
        try:
            return await coro
        finally:
            await close_pool()

    return asyncio.run(wrapper())


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _fmt_date(d) -> str:
    return d.strftime("%Y-%m-%d") if d else "—"


# ---------------------------------------------------------------------------
@app.command("db-init")
def db_init() -> None:
    """Execute schema.sql (extensions, tables, indexes, views) against DATABASE_URL."""

    async def go():
        await init_schema(await get_pool())

    _run(go())
    console.print("[green]Schema applied.[/green]")


@app.command("db-init-web")
def db_init_web() -> None:
    """Execute schema_web.sql + schema_web_v15.sql .. v17.sql (web UI, Stage 3, worker control, triage,
    source weighting, official records, merge "not duplicate" decisions). Mirrors sql/ v1.2 .. v1.9 (v1.8: measures + roll-call votes; v1.9: coalition index
    ignores take-part-only events)."""

    async def go():
        pool = await get_pool()
        await init_schema(pool, "schema_web.sql")
        await init_schema(pool, "schema_web_v15.sql")
        await init_schema(pool, "schema_web_v16.sql")
        await init_schema(pool, "schema_web_v17.sql")
        await init_schema(pool, "schema_web_v18.sql")
        await init_schema(pool, "schema_web_v19.sql")
        await init_schema(pool, "schema_web_v20.sql")
        await init_schema(pool, "schema_web_v21.sql")

    _run(go())
    console.print("[green]Web schema applied.[/green]")


@app.command("source-add")
def source_add(
    name: str = typer.Option(..., help="Display name, e.g. 'Davis Vanguard'"),
    url: str = typer.Option(..., help="Base URL, e.g. https://davisvanguard.org"),
    sitemap: Optional[str] = typer.Option(None, help="Sitemap index URL"),
    rss: Optional[str] = typer.Option(None, help="RSS/Atom feed URL"),
    config: Optional[str] = typer.Option(
        None, help='scrape_config JSON, e.g. \'{"method":"links","article_pattern":"/20\\\\d{2}/"}\''),
    inactive: bool = typer.Option(False, "--inactive", help="Register the source but skip it when crawling."),
) -> None:
    """Add (or update) a news source."""
    import json

    cfg = json.loads(config) if config else None

    async def go():
        async with (await get_pool()).connection() as conn:
            return await repo.add_source(conn, name, url, sitemap, rss, cfg, not inactive)

    row = _run(go())
    state = "" if row["is_active"] else " [yellow](inactive)[/yellow]"
    console.print(f"[green]Source #{row['id']}[/green] {row['name']} → {row['base_url']}{state}")


@app.command("source-list")
def source_list() -> None:
    """List configured sources."""

    async def go():
        async with (await get_pool()).connection() as conn:
            return await repo.list_sources(conn, active_only=False)

    t = Table("ID", "Name", "Base URL", "Method", "Active", "Last crawled", "Note")
    for s in _run(go()):
        cfg = s["scrape_config"] or {}
        t.add_row(str(s["id"]), s["name"], s["base_url"], cfg.get("method", "auto"),
                  "yes" if s["is_active"] else "no", _fmt_date(s["last_crawled_at"]), (cfg.get("note") or "")[:60])
    console.print(t)


@app.command()
def crawl(
    source_id: Optional[int] = typer.Option(None, help="Only crawl this source."),
    backfill_days: Optional[int] = typer.Option(None, help="Retroactively seed the last N days from sitemaps."),
    since: Optional[str] = typer.Option(None, help="Retroactively seed from YYYY-MM-DD."),
    until: Optional[str] = typer.Option(None, help="Upper bound YYYY-MM-DD for backfill."),
    limit: Optional[int] = typer.Option(None, help="Max new articles to download per source."),
) -> None:
    """Crawl sources. Without --backfill-days/--since, does an incremental (RSS + recent sitemap) crawl."""
    from datetime import date

    from civic_tracker.crawler.scraper import crawl_source, for_each_source, window_from_args

    start = window_from_args(backfill_days, since)
    end = date.fromisoformat(until) if until else None
    backfill = start is not None

    async def go():
        pool = await get_pool()
        async with pool.connection() as conn:
            sources = await repo.list_sources(conn, source_id)
        done = {}

        async def one(s):
            console.print(f"Crawling [bold]{s['name']}[/bold] ({'backfill since ' + str(start) if backfill else 'recent'})…")
            done[s["id"]] = await crawl_source(pool, s, since=start, until=end, backfill=backfill, limit=limit)
            console.print(f"  finished [bold]{s['name']}[/bold]")

        await for_each_source(sources, one)  # different sites at once; same-host sources one after another
        return [(s, done[s["id"]]) for s in sources if s["id"] in done]

    results = _run(go())
    if not results:
        console.print("[yellow]No active sources. Add one with `civic-tracker source-add`.[/yellow]")
        return
    t = Table("Source", "Method", "Discovered", "New", "Inserted", "Out of window", "No date", "Dup content", "Too short", "Robots", "Errors", "HTTP reqs", "429s")
    for s, st in results:
        t.add_row(s["name"], st.method, *(str(x) for x in (st.discovered, st.new_urls, st.inserted, st.out_of_window,
                                                  st.no_date, st.duplicate_content, st.too_short, st.disallowed, st.errors,
                                                  st.requests, st.throttled)))
    console.print(t)
    for _, st in results:
        for e in st.error_samples:
            console.print(f"[red]  {e}[/red]")


@app.command()
def process(
    concurrency: int = typer.Option(6, help="Parallel article slots (concurrent LLM requests)."),
    max_articles: Optional[int] = typer.Option(None, help="Stop after N articles."),
) -> None:
    """Drain the queue once: newest PENDING/STAGE1_DONE articles first (the worker does this continuously)."""
    from civic_tracker.config import settings
    from civic_tracker.pipeline.orchestrator import Orchestrator
    from civic_tracker.pipeline.triage import Triage

    async def go():
        orch = Orchestrator(await get_pool(), concurrency=concurrency,
                            triage=Triage() if settings.TRIAGE_ENABLED else None)
        return await orch.run(max_articles=max_articles)

    st = _run(go())
    t = Table("Processed", "Skipped", "Failed", "Events created", "Events merged")
    t.add_row(str(st.processed), str(st.skipped), str(st.failed), str(st.events_created), str(st.events_merged))
    console.print(t)
    for aid, err in st.failures[:10]:
        console.print(f"[red]  article {aid}: {err}[/red]")


@app.command()
def worker(
    slots: int = typer.Option(None, help="Parallel article slots (default: WORKER_SLOTS)."),
) -> None:
    """Run the 24/7 pipeline worker: scheduled crawls (queue only), continuous newest-first processing,
    Stage 3 bios when idle, and admin commands from the web UI. Stop with SIGTERM/Ctrl-C."""
    from civic_tracker.config import settings
    from civic_tracker.worker import run_worker

    logging.getLogger().setLevel(logging.INFO)
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "openai", "urllib3", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(run_worker(slots or settings.WORKER_SLOTS))


@app.command("entity-cleanup")
def entity_cleanup(
    apply: bool = typer.Option(False, "--apply", help="Execute the plan (default: dry run + report only)."),
    report: str = typer.Option("ENTITY_CLEANUP_REPORT.md", help="Where to write the markdown plan."),
) -> None:
    """Repair local-body entities: remove wrong aliases ("Davis" on Davis City Council, Fire on Police,
    West Sacramento on Sacramento) and split catch-alls like "Planning Commission" into each city's body.
    Dry run by default; --apply runs the same plan in one transaction."""
    from pathlib import Path

    from civic_tracker.pipeline import entity_cleanup as ec

    async def go():
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                p = await ec.plan(conn)
                Path(report).write_text(ec.report(p))
                return p, (await ec.apply(conn, p) if apply else None)

    p, counts = _run(go())
    console.print(f"Plan written to [bold]{report}[/bold]: {len(p.alias_fixes)} aliases, {len(p.moves)} rows to move, "
                  f"{len(p.unplaced)} unplaced, {len(p.deletes)} catch-alls to delete.")
    if counts is not None:
        console.print(f"[green]Applied:[/green] {counts}")
    else:
        console.print("Dry run - nothing changed. Re-run with --apply to execute.")


@app.command("triage-backfill")
def triage_backfill(
    limit: int = typer.Option(500, help="Max articles to label."),
    concurrency: int = typer.Option(4, help="Parallel classifier calls to the local LLM."),
) -> None:
    """Label already-PROCESSED articles with genre + Yolo relevance (labels only; nothing is skipped or
    re-processed). Needed so source weighting can tell opinion pieces from news for older articles."""
    from civic_tracker.pipeline.triage import Triage

    async def go():
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT a.id, a.url, a.title, a.full_text, s.name, s.scrape_config
                   FROM TB_Article a JOIN TB_Sources s ON s.id = a.source_id
                   WHERE a.processing_status = 'PROCESSED' AND a.triaged_at IS NULL
                   ORDER BY a.published_at DESC NULLS LAST LIMIT %s""", (limit,))
            rows = await cur.fetchall()
        t, sem, counts = Triage(), asyncio.Semaphore(concurrency), {}

        async def one(r):
            async with sem:
                res = await t.check(url=r["url"], title=r["title"], text=r["full_text"],
                                    source={"name": r["name"], "scrape_config": r["scrape_config"]})
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE TB_Article SET genre = %s, yolo_relevance = %s, triaged_at = NOW() WHERE id = %s",
                    (res.genre, res.relevance, r["id"]))
            counts[res.genre or "unlabelled"] = counts.get(res.genre or "unlabelled", 0) + 1

        await asyncio.gather(*(one(r) for r in rows))
        return len(rows), counts

    n, counts = _run(go())
    console.print(f"Labelled {n} processed article(s): {counts}")


@app.command()
def reextract(
    genre: Optional[str] = typer.Option("opinion", help="Re-extract PROCESSED articles of this genre."),
    article_id: Optional[list[int]] = typer.Option(None, "--article-id", help="Specific article id(s) instead."),
    kind: Optional[str] = typer.Option(None, help="Only official records of this doc_meta kind, e.g. 'minutes' "
                                                  "(implies --genre official_record)."),
    via: str = typer.Option("burst", help="'burst' (paid API endpoint) or 'local' (local LLM server)."),
    max_spend: float = typer.Option(2.0, help="Stop claiming new articles once API spend reaches this (USD)."),
    concurrency: int = typer.Option(8, help="Parallel requests."),
) -> None:
    """Re-run Stage 2 + resolver on already processed articles (e.g. opinion pieces after the evidence-type
    prompt change). Old stance rows for each article are replaced atomically; disputed rows are kept;
    events/entities left without support are deleted. Safe to run while the worker is running."""
    from civic_tracker.pipeline.encoder import LocalEncoderClient
    from civic_tracker.pipeline.orchestrator import Orchestrator, RunStats
    from civic_tracker.pipeline.stage1_nlp import Stage1Processor
    from civic_tracker.config import settings
    from civic_tracker.pipeline.stage2_llm import DeepSeekExtractor, TruncationFallbackExtractor

    async def go():
        pool = await get_pool()
        async with pool.connection() as conn:
            if article_id:
                ids = list(article_id)
            elif kind:
                cur = await conn.execute(
                    "SELECT id FROM TB_Article WHERE processing_status = 'PROCESSED' AND genre = 'official_record' "
                    "AND doc_meta->>'kind' = %s ORDER BY published_at DESC NULLS LAST", (kind,))
                ids = [r["id"] for r in await cur.fetchall()]
            else:
                cur = await conn.execute(
                    "SELECT id FROM TB_Article WHERE processing_status = 'PROCESSED' AND genre = %s "
                    "ORDER BY published_at DESC NULLS LAST", (genre,))
                ids = [r["id"] for r in await cur.fetchall()]
        # a truncated output gets one more try (local: on the API model, within the daily cap)
        if via == "burst":
            extractor = TruncationFallbackExtractor(DeepSeekExtractor.burst())
        else:
            extractor = TruncationFallbackExtractor(
                DeepSeekExtractor(), DeepSeekExtractor.burst if settings.BURST_LLM_API_KEY else None)
        orch = Orchestrator(pool, stage1=Stage1Processor(), extractor=extractor, encoder=LocalEncoderClient(),
                            concurrency=concurrency)
        stats = RunStats()
        console.print(f"Re-extracting {len(ids)} article(s) via {extractor.provider} ({extractor.model}) ...")
        await orch.reextract(ids, stats, should_stop=lambda: extractor.spent_usd >= max_spend)
        return ids, stats, extractor.spent_usd

    ids, stats, spent = _run(go())
    for line in stats.log_lines[-60:]:
        console.print(line)
    console.print(f"[bold]Done:[/bold] {stats.processed} re-extracted, {stats.failed} failed, "
                  f"{len(ids) - stats.processed - stats.failed} not run; spend ${spent:.4f}")


@app.command("assign-sides")
def assign_sides_cmd(
    limit: int = typer.Option(500, help="Events to check this run (contested kinds and newest first)."),
    event_id: Optional[int] = typer.Option(None, "--event", help="Only this event."),
    max_spend: Optional[float] = typer.Option(None, help="USD cap for this run (default SIDES_BACKFILL_SPEND_CAP_USD)."),
    show_prompt: bool = typer.Option(False, help="Print the prompt for --event and exit (no API call)."),
) -> None:
    """Backfill event SIDES (v2.0) for events extracted before the sides prompt, on the burst API model.
    Only side tables are written; stances, quotes and summaries are untouched. Safe while the worker runs."""
    from civic_tracker.pipeline.assign_sides import ACTORS_SQL, MAX_ACTORS, SidesBackfill, build_prompt

    async def go():
        pool = await get_pool()
        if show_prompt:
            async with pool.connection() as conn:
                ev = await (await conn.execute("SELECT * FROM TB_Event WHERE id = %s", (event_id,))).fetchone()
                actors = await (await conn.execute(ACTORS_SQL, (event_id, MAX_ACTORS))).fetchall()
            console.print(build_prompt(ev, actors))
            return
        stats = await SidesBackfill(pool, cap_usd=max_spend).run(limit=limit, event_id=event_id)
        console.print(stats.summary())
        for e in stats.errors[:10]:
            console.print(f"  [red]{e}[/red]")

    _run(go())


@app.command("synthesize-wiki")
def synthesize_wiki(
    limit: int = typer.Option(100, help="Max dirty entities to (re)write."),
    entity_id: Optional[int] = typer.Option(None, "--entity-id", help="Regenerate one entity (ignores threshold)."),
) -> None:
    """Stage 3: write Wikipedia-style lead bios for eligible dirty entities."""
    from civic_tracker.pipeline.stage3_wiki import WikiBioWriter, eligible_dirty_entities, synthesize_entity

    async def go():
        pool = await get_pool()
        writer = WikiBioWriter()
        async with pool.connection() as conn:
            ids = [entity_id] if entity_id else await eligible_dirty_entities(conn, limit)
        done = failed = 0
        for i, eid in enumerate(ids, 1):
            console.print(f"[{i}/{len(ids)}] entity {eid}")
            try:
                async with pool.connection() as conn:
                    done += await synthesize_entity(conn, writer, eid, pool=pool)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                console.print(f"[red]  entity {eid}: {type(exc).__name__}: {exc}[/red]")
        return done, failed, len(ids)

    done, failed, n = _run(go())
    console.print(f"Bios written: {done}/{n}, failed: {failed}")


@app.command()
def dossier(
    entity_name: str = typer.Argument(..., help="Entity name or alias."),
    topic: Optional[str] = typer.Option(None, help="Filter ledger by topic tag, e.g. 'immigration'."),
    limit: int = typer.Option(50, help="Max ledger rows."),
) -> None:
    """Career timeline, chronological stance ledger, and coalition allies/opponents for an entity."""

    async def go():
        async with (await get_pool()).connection() as conn:
            ent = await repo.find_entity_by_name(conn, entity_name)
            if not ent:
                return None
            return (
                ent,
                await repo.get_affiliations(conn, ent["id"]),
                await repo.get_career_ledger(conn, ent["id"], topic),
                await repo.get_coalition(conn, ent["id"], allies=True),
                await repo.get_coalition(conn, ent["id"], allies=False),
            )

    data = _run(go())
    if data is None:
        console.print(f"[red]No entity matching '{entity_name}'.[/red]")
        raise typer.Exit(1)
    ent, affs, ledger, allies, opponents = data

    aliases = ", ".join(ent["aliases"] or []) or "—"
    console.print(Panel(f"[bold]{ent['canonical_name']}[/bold]  ({ent['entity_type']})\nAliases: {aliases}",
                        title=f"Dossier #{ent['id']}"))

    t = Table("Role", "Organization", "First seen", "Last seen", title="1. Career Roles Timeline")
    for a in affs:
        t.add_row(a["role_title"], a["organization"] or "—", _fmt_date(a["first_seen_date"]), _fmt_date(a["last_seen_date"]))
    console.print(t)

    t = Table("Date", "Event", "Stance", "Role / Action", "Quotes", "Source",
              title="2. Event & Position Ledger" + (f" — topic: {topic}" if topic else ""), show_lines=True)
    for r in ledger[:limit]:
        quotes = "\n".join(f"“{q}”" for q in (r["direct_quotes"] or []))
        t.add_row(_fmt_date(r["effective_date"]), f"{r['event_title']}\n[dim]{', '.join(r['topic_tags'] or [])}[/dim]",
                  r["stance"], f"[italic]{r['role_in_event']}[/italic]\n{r['action_summary']}", quotes or "—", r["source_url"])
    console.print(t)

    for title, rows in (("3a. Top Coalition Allies", allies), ("3b. Frequent Counterparts / Opponents", opponents)):
        t = Table("Entity", "Type", "Shared events", "Aligned", "Opposed", "Alignment", "Shared topics", title=title)
        for r in rows:
            t.add_row(r["entity_b_name"], r["entity_b_type"], str(r["shared_events_count"]), str(r["aligned_stance_count"]),
                      str(r["opposing_stance_count"]), f"{r['alignment_index']:+.3f}", ", ".join(r["shared_topic_tags"] or []))
        console.print(t)


@app.command("search-positions")
def search_positions(
    query: str = typer.Argument(..., help="Natural-language policy/stance query."),
    entity: Optional[str] = typer.Option(None, help="Restrict to one entity (name or alias)."),
    limit: int = typer.Option(20, help="Max results."),
) -> None:
    """Semantic search over recorded actions, stances, and quotes."""
    from civic_tracker.pipeline.encoder import LocalEncoderClient

    async def go():
        [qvec] = await LocalEncoderClient().embed([query], task="SEARCH_QUERY")
        async with (await get_pool()).connection() as conn:
            eid = None
            if entity:
                ent = await repo.find_entity_by_name(conn, entity)
                if not ent:
                    return None
                eid = ent["id"]
            return await repo.search_positions(conn, qvec, eid, limit)

    rows = _run(go())
    if rows is None:
        console.print(f"[red]No entity matching '{entity}'.[/red]")
        raise typer.Exit(1)
    t = Table("Score", "Entity", "Date", "Event", "Stance", "Action / Quotes", "Source", show_lines=True)
    for r in rows:
        quotes = " ".join(f"“{q}”" for q in (r["direct_quotes"] or []))
        t.add_row(f"{r['score']:.3f}", r["canonical_name"], _fmt_date(r["event_date"]), r["canonical_title"],
                  r["stance"], r["action_summary"] + (f"\n[dim]{quotes}[/dim]" if quotes else ""), r["source_url"])
    console.print(t)


if __name__ == "__main__":
    app()
