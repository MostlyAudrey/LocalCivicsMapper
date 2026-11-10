# SPEC_WEB_UI.md: Civic Wiki Frontend, Salience-Ranked Stage 3 Synthesizer, Admin Backend, & In-Process Scheduler

## 1. Architectural Overview
Extend the `civic_tracker` application into a single, continuously running 24/7 service (`civic-tracker serve --with-scheduler`) backed by PostgreSQL (`pgvector` + `pg_trgm`):

1. **Database Observability, Salience Views, & Wiki State (`db/schema_web.sql`)**:
   - Add pipeline run telemetry (`TB_Job_Run`), dirty-flag tracking (`bio_is_dirty`, `bio_updated_at`) on `TB_Entity`, and `VW_Entity_Salient_Career_Events` to score and rank career events by multi-factor relevance rather than raw recency.
2. **Stage 3 Incremental Wiki Lead Synthesizer (`pipeline/stage3_wiki.py`)**:
   - A post-processing pass using `DeepSeek V4-flash` that generates neutral, 2–3 paragraph Wikipedia-style executive summaries (`TB_Entity.summary_bio`) with `[[Wiki Link]]` syntax **only** for entities modified in the latest batch (`bio_is_dirty = TRUE`), fed by their top 20 most salient career events.
3. **In-Process `APScheduler` Daemon (`scheduler.py`)**:
   - Runs inside the FastAPI `lifespan` (`--workers 1`), keeping `fastcoref`, `GLiNER`, and `spaCy` singleton models warm in RAM.
   - Executes the chained pipeline (`Crawl -> Stage 1 & 2 Parse -> Stage 3 Wiki Synthesis`) on a configurable interval, offloading CPU-bound Stage 1 PyTorch calls via `asyncio.to_thread()` so the web UI never blocks.
4. **Public "Civic Wiki" Web Application (`web/public.py`)**:
   - Server-rendered Wiki UI built with **FastAPI + Jinja2 + HTMX + TailwindCSS** featuring live omnibox search (lexical `pg_trgm` + semantic `Qwen3-Embedding-4B`), Entity Dossier pages, and Event Dossier pages.
5. **Password-Protected Admin Dashboard & Alerting (`web/admin.py`)**:
   - Session-authenticated backend (`/admin`) to manage `TB_Sources`, launch immediate crawls or historical backfills, view `TB_Job_Run` logs, inspect/retry failed articles, merge duplicate entities, pause/resume the scheduler, and push webhook failure alerts.

---

## 2. Additional Dependencies (`pyproject.toml`)
Ensure the following dependencies are installed in the environment:
- `fastapi>=0.115.0`
- `uvicorn[standard]>=0.30.0`
- `jinja2>=3.1.4`
- `python-multipart>=0.0.9`
- `itsdangerous>=2.2.0`
- `apscheduler>=3.10.4`

Update `civic_tracker/config.py` (`Settings`) with:
```python
    ADMIN_PASSWORD: str = "changeme_in_env"
    SESSION_SECRET_KEY: str = "change-this-secret-signing-key"
    ALERT_WEBHOOK_URL: Optional[str] = None  # Mattermost / Slack / Discord / Home Assistant JSON webhook
    WEB_HOST: str = "0.0.0.0"
    WEB_PORT: int = 8080
    ENABLE_SCHEDULER: bool = True
    SCHEDULE_INTERVAL_HOURS: float = 6.0
```

---

## 3. Database Schema Additions & Salience Views (`civic_tracker/db/schema_web.sql`)

```sql
-- 1. Dirty flag and timestamp on TB_Entity for incremental Stage 3 Wiki synthesis
ALTER TABLE TB_Entity
    ADD COLUMN IF NOT EXISTS bio_is_dirty BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS bio_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_entity_bio_dirty ON TB_Entity(bio_is_dirty) WHERE bio_is_dirty = TRUE;

-- 2. Pipeline & Crawler Job Telemetry Table for Admin UI & Alerting
CREATE TABLE IF NOT EXISTS TB_Job_Run (
    id SERIAL PRIMARY KEY,
    job_type TEXT NOT NULL CHECK (job_type IN ('FULL_CYCLE', 'CRAWL', 'BACKFILL', 'PROCESS_PIPELINE', 'WIKI_SYNTHESIS')),
    source_id INTEGER REFERENCES TB_Sources(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL_FAILURE', 'FAILED')),
    items_processed INTEGER DEFAULT 0,
    items_failed INTEGER DEFAULT 0,
    log_output TEXT DEFAULT '',
    error_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_run_started ON TB_Job_Run(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_run_status ON TB_Job_Run(status);

-- 3. Event-Deduplicated & Salience-Ranked Career Ledger for Wiki Synthesis & UI Toggle
CREATE OR REPLACE VIEW VW_Entity_Salient_Career_Events AS
WITH event_gravity AS (
    SELECT
        ev.id AS event_id,
        COUNT(DISTINCT ae.article_id) AS total_event_articles,
        COUNT(DISTINCT ee.entity_id) AS total_event_actors
    FROM TB_Event ev
    LEFT JOIN TB_Article_Event ae ON ae.event_id = ev.id
    LEFT JOIN TB_Entity_Event ee ON ee.event_id = ev.id
    GROUP BY ev.id
),
entity_event_rollup AS (
    SELECT
        ee.entity_id,
        ee.event_id,
        COUNT(DISTINCT ee.source_article_id) AS entity_article_citations,
        (ARRAY_AGG(
            ee.stance ORDER BY
            CASE WHEN ee.stance IN ('SUPPORT', 'OPPOSE', 'APOLOGETIC', 'DEFENSIVE', 'INVESTIGATIVE') THEN 1 ELSE 2 END,
            cardinality(ee.direct_quotes) DESC,
            ee.confidence DESC
        ))[1] AS primary_stance,
        (ARRAY_AGG(
            ee.role_in_event ORDER BY length(ee.role_in_event) DESC
        ))[1] AS primary_role_in_event,
        STRING_AGG(DISTINCT ee.action_summary, ' | ') AS combined_action_summary,
        ARRAY_REMOVE(ARRAY_AGG(DISTINCT q), NULL) AS all_direct_quotes,
        ARRAY_AGG(DISTINCT ee.source_article_id) AS source_article_ids
    FROM TB_Entity_Event ee
    LEFT JOIN LATERAL unnest(ee.direct_quotes) AS q ON TRUE
    GROUP BY ee.entity_id, ee.event_id
),
scored_events AS (
    SELECT
        r.entity_id,
        e.canonical_name,
        ev.id AS event_id,
        ev.canonical_title AS event_title,
        ev.event_type,
        ev.event_date,
        ev.topic_tags,
        COALESCE(ev.topic_tags[1], 'general') AS primary_topic,
        r.entity_article_citations,
        eg.total_event_articles,
        eg.total_event_actors,
        r.primary_role_in_event,
        r.primary_stance,
        r.combined_action_summary,
        r.all_direct_quotes,
        r.source_article_ids,
        ROUND((
            (3.0 * LN(1 + r.entity_article_citations)) +
            (1.5 * LN(1 + eg.total_event_articles)) +
            (1.2 * LN(1 + eg.total_event_actors)) +
            (CASE
                WHEN r.primary_stance IN ('SUPPORT', 'OPPOSE', 'APOLOGETIC', 'DEFENSIVE', 'INVESTIGATIVE') THEN 2.0
                ELSE 0.0
             END) +
            (CASE WHEN COALESCE(cardinality(r.all_direct_quotes), 0) > 0 THEN 1.5 ELSE 0.0 END) +
            (CASE
                WHEN ev.event_type IN ('LEGISLATIVE_VOTE', 'POLICY_ANNOUNCEMENT', 'PUBLIC_CONTROVERSY', 'LEGAL_ACTION') THEN 1.0
                ELSE 0.0
             END) +
            (1.5 / (1.0 + GREATEST(0, (CURRENT_DATE - COALESCE(ev.event_date, CURRENT_DATE)))::NUMERIC / 365.0))
        )::NUMERIC, 3) AS salience_score
    FROM entity_event_rollup r
    JOIN TB_Entity e ON e.id = r.entity_id
    JOIN TB_Event ev ON ev.id = r.event_id
    JOIN event_gravity eg ON eg.event_id = ev.id
)
SELECT
    *,
    ROW_NUMBER() OVER (
        PARTITION BY entity_id, primary_topic
        ORDER BY salience_score DESC, event_date DESC NULLS LAST
    ) AS topic_rank
FROM scored_events;
```

---

## 4. Stage 3: Incremental Wiki Lead Synthesizer (`civic_tracker/pipeline/stage3_wiki.py`)

### Why We Don't Use the LLM to Render the Whole Page
The bottom 85% of every Wiki page (career timeline, coalition alignment matrix, salience-ranked or chronological stance ledger, and verbatim quotes) is rendered deterministically from PostgreSQL (`VW_Entity_Salient_Career_Events`, `VW_Entity_Career_Ledger`, and `VW_Entity_Coalition_Matrix`). This guarantees zero hallucination, instant updates, and intact primary-source citations.

Stage 3 uses `DeepSeek V4-flash` **only** to synthesize `TB_Entity.summary_bio`—a 2-to-3 paragraph Wikipedia-style lead overview at the top of the page, driven by the multi-factor **Salience Score** so career-defining controversies, votes, and coalitions are never overwritten by minor recent routine activity.

### Stage 3 Execution Logic
1. **Dirty-Flag Trigger**: In `resolver.py`, whenever a `TB_Entity_Event` or `TB_Entity_Affiliation` row is inserted or updated for `entity_id`, execute:
   ```sql
   UPDATE TB_Entity SET bio_is_dirty = TRUE WHERE id = %(entity_id)s;
   ```
2. **Salience-Driven Context Retrieval (`synthesize_dirty_bios(limit=100)`)**:
   - Query `TB_Entity WHERE bio_is_dirty = TRUE` (ordered by total event count DESC, `id` ASC).
   - For each dirty entity, fetch three structured datasets from PostgreSQL:
     1. **Career Roles (`TB_Entity_Affiliation`)**: All roles, linked organization names, `first_seen_date`, and `last_seen_date` ordered chronologically.
     2. **Top 20 Most Salient Career Events (`VW_Entity_Salient_Career_Events`)**: Select the top 20 defining events using `topic_rank <= 5` (so no single policy topic monopolizes all 20 slots), and then re-order those selected 20 events chronologically (`event_date ASC`) so the LLM receives a coherent timeline weighted by importance:
        ```sql
        WITH top_salient AS (
            SELECT
                event_date,
                event_title,
                event_type,
                topic_tags,
                entity_article_citations,
                total_event_articles,
                primary_role_in_event,
                primary_stance,
                combined_action_summary,
                all_direct_quotes,
                salience_score
            FROM VW_Entity_Salient_Career_Events
            WHERE entity_id = %(entity_id)s
              AND topic_rank <= 5
            ORDER BY salience_score DESC, event_date DESC NULLS LAST
            LIMIT 20
        )
        SELECT * FROM top_salient
        ORDER BY event_date ASC NULLS LAST, salience_score DESC;
        ```
     3. **Coalition Allies & Counterparts (`VW_Entity_Coalition_Matrix`)**: Top 5 allies (`alignment_index > 0`, ordered by `shared_events_count DESC, alignment_index DESC`) and top 5 opponents (`alignment_index < 0`, ordered by `opposing_stance_count DESC`).
3. **Structured Synthesis via `DeepSeek V4-flash`**:
   - Format each of the 20 salient events in the prompt with its salience metadata:
     `[{event_date}] {event_title} (Salience: {salience_score} | Cited in {entity_article_citations}/{total_event_articles} articles | Topics: {topic_tags}) — Role: {primary_role_in_event} [{primary_stance}]: {combined_action_summary}`
   - Instruct `DeepSeek V4-flash` to allocate prose proportionally to `salience_score` and `entity_article_citations`.
   - Enforce guided JSON output via Pydantic:
     ```python
     class WikiBioSynthesis(BaseModel):
         lead_markdown: str = Field(
             description=(
                 "2 to 3 concise, objective, encyclopedic paragraphs summarizing this entity's "
                 "career roles, highest-salience policy actions, major controversies, and "
                 "recurring coalition partners or opponents. Weight coverage toward events "
                 "with the highest article citation counts and salience scores. "
                 "Wrap any mentioned canonical person, government body, or organization name "
                 "in double brackets like [[Gloria Ravel]] or [[Davis Police Department]] "
                 "for automatic internal wiki linking."
             )
         )
     ```
   - Save `lead_markdown` to `TB_Entity.summary_bio`, set `bio_is_dirty = FALSE`, and set `bio_updated_at = NOW()`.

### Wiki-Link Auto-Resolver (`civic_tracker/web/filters.py`)
Create a Jinja2 filter `render_wiki_links(text: str, entity_lookup: dict[str, int]) -> Markup`:
- Parses `[[Entity Name]]` patterns in `summary_bio` or `action_summary`.
- Matches `Entity Name` (case-insensitive against `TB_Entity.canonical_name` and `unnest(aliases)`) to its `TB_Entity.id` and replaces it with `<a href="/wiki/entity/{id}" class="text-blue-600 hover:underline font-medium">{Entity Name}</a>` (or plain text if no matching entity exists).

---
## 5. In-Process Worker Manager & Live Admin Controls (`civic_tracker/scheduler.py`)

### 1. Persistent Scheduler Configuration Table (Add to `db/schema_web.sql`)
Store the scheduler's enabled/disabled toggle and interval in PostgreSQL so toggling the job off in `/admin` persists across container restarts:

```sql
CREATE TABLE IF NOT EXISTS TB_Scheduler_State (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1), -- Enforces single-row table
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    interval_hours FLOAT NOT NULL DEFAULT 6.0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO TB_Scheduler_State (id, is_enabled, interval_hours)
VALUES (1, TRUE, 6.0)
ON CONFLICT (id) DO NOTHING;

-- Allow 'CANCELLED' status in TB_Job_Run when stopped from the Admin UI
ALTER TABLE TB_Job_Run DROP CONSTRAINT IF EXISTS tb_job_run_status_check;
ALTER TABLE TB_Job_Run ADD CONSTRAINT tb_job_run_status_check
    CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL_FAILURE', 'FAILED', 'CANCELLED'));
```

### 2. Trackable Worker & Cancellation Manager (`civic_tracker/scheduler.py`)
Instead of anonymous FastAPI `BackgroundTasks`, manage all scheduled and manual runs through a singleton `PipelineWorkerManager` that tracks the active `asyncio.Task`, live progress string, and a cooperative `cancel_event`:

```python
import asyncio
from datetime import datetime, timezone
from typing import Optional, Literal
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

WorkerState = Literal["IDLE", "RUNNING", "STOPPING"]

class PipelineWorkerManager:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()
        self.current_task: Optional[asyncio.Task] = None
        self.cancel_event = asyncio.Event()
        self.state: WorkerState = "IDLE"
        self.current_stage: Optional[str] = None      # e.g., "CRAWL", "STAGE1_2_PARSE", "WIKI_SYNTHESIS"
        self.progress_detail: str = "Idle"            # e.g., "Parsing article 12 of 35..."
        self.active_job_run_id: Optional[int] = None
        self.started_at: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        return self.current_task is not None and not self.current_task.done()

    @property
    def is_schedule_enabled(self) -> bool:
        job = self.scheduler.get_job("civic_full_cycle")
        return bool(job and job.next_run_time is not None)

    @property
    def next_run_time(self) -> Optional[datetime]:
        job = self.scheduler.get_job("civic_full_cycle")
        return job.next_run_time if job else None

    def update_progress(self, stage: str, detail: str, job_run_id: Optional[int] = None) -> None:
        self.current_stage = stage
        self.progress_detail = detail
        if job_run_id is not None:
            self.active_job_run_id = job_run_id

    def check_cancelled(self) -> None:
        """Call between articles/batches inside crawler, stage1_2, and stage3 loops."""
        if self.cancel_event.is_set():
            raise asyncio.CancelledError("Pipeline job stopped by admin via Web UI.")

    async def start_cycle(self, job_type: str = "FULL_CYCLE", source_id: Optional[int] = None, backfill_days: Optional[int] = None) -> bool:
        if self.is_running:
            return False  # Already running
        self.cancel_event.clear()
        self.state = "RUNNING"
        self.started_at = datetime.now(timezone.utc)
        self.current_task = asyncio.create_task(
            self._execute_wrapper(job_type=job_type, source_id=source_id, backfill_days=backfill_days)
        )
        return True

    async def stop_active_job(self) -> bool:
        """Signals cooperative cancellation and cancels the asyncio task if hung on network I/O."""
        if not self.is_running:
            return False
        self.state = "STOPPING"
        self.progress_detail = "Stopping after current item finishes..."
        self.cancel_event.set()
        # Give cooperative loop 3 seconds to exit cleanly before forcing task cancellation
        try:
            await asyncio.wait_for(asyncio.shield(self.current_task), timeout=3.0)
        except asyncio.TimeoutError:
            if self.current_task:
                self.current_task.cancel()
        except asyncio.CancelledError:
            pass
        return True

    async def set_schedule_state(self, enabled: bool, interval_hours: Optional[float] = None) -> None:
        """Toggles the recurring APScheduler job on/off and persists to TB_Scheduler_State."""
        await db_save_scheduler_state(enabled=enabled, interval_hours=interval_hours)
## 5. In-Process 24/7 Scheduler & Model Singleton Management (`civic_tracker/scheduler.py`)
        job = self.scheduler.get_job("civic_full_cycle")
        if interval_hours and job:
            job.reschedule(trigger=IntervalTrigger(hours=interval_hours))
        if enabled:
            self.scheduler.resume_job("civic_full_cycle")
        else:
            self.scheduler.pause_job("civic_full_cycle")

    async def _execute_wrapper(self, job_type: str, source_id: Optional[int], backfill_days: Optional[int]) -> None:
        try:
            await run_pipeline_workflow(
                worker=self,
                job_type=job_type,
                source_id=source_id,
                backfill_days=backfill_days,
            )
        except asyncio.CancelledError:
            if self.active_job_run_id:
                await mark_job_run_cancelled(self.active_job_run_id, "Stopped manually by admin.")
        finally:
            self.state = "IDLE"
            self.current_stage = None
            self.progress_detail = "Idle"
            self.active_job_run_id = None
            self.started_at = None

worker_manager = PipelineWorkerManager()
```

### 3. Cooperative Checkpoints in the Pipeline Loops
Inside the crawler loop, the Stage 1/2 article batch loop, and the Stage 3 entity synthesis loop:
- Call `worker_manager.check_cancelled()` before starting each article or entity.
- Call `worker_manager.update_progress("PROCESS_PIPELINE", f"Processing article {idx}/{total}: {article.title[:50]}...", job_run_id)` so the `/admin` dashboard displays real-time progress.

### 4. Live HTMX Admin Control Panel (`civic_tracker/web/admin.py`)
Add an HTMX live-polling status card at the top of `/admin` (`hx-get="/admin/hx/worker-status" hx-trigger="load, every 2s"`):
- **When `worker_manager.state == "RUNNING"`**:
  - Displays a pulsing green/amber badge: `RUNNING — [STAGE]`, elapsed time since `started_at`, and the live `progress_detail` text (e.g., *"Processing article 14/38: Davis Mayor Apologizes..."*).
  - Renders a red **`⏹ Stop Running Job`** button (`hx-post="/admin/worker/stop" hx-target="#worker-status-panel"`).
- **When `worker_manager.state == "IDLE"`**:
  - Displays `IDLE`, the timestamp of the last completed run, and a green **`▶ Run Full Cycle Now`** button (`hx-post="/admin/worker/start" hx-target="#worker-status-panel"`).
- **Schedule Master Toggle Card**:
  - Shows a toggle switch (`Auto-Schedule: ON / OFF`, `hx-post="/admin/scheduler/toggle"`), the next scheduled run countdown (`next_run_time`), and an inline form to update `interval_hours` (e.g., every `6` hours) without restarting the container.
---

## 6. Public Civic Wiki Frontend (`civic_tracker/web/public.py`)

Use **FastAPI + Jinja2 + HTMX + TailwindCSS (via CDN)** for a clean, responsive Wikipedia / Ballotpedia layout.

### Routes & Pages
1. **`GET /` (Wiki Home & Activity Feed)**:
   - **Omnibox Search Bar**: Instant HTMX live-search dropdown (`GET /hx/search?q=...`) plus a toggle for Semantic Position Search.
   - **Recent Civic Events Stream**: Cards showing the latest 15 extracted `TB_Event` records, event date, topic tags, involved entity pills (color-coded by stance), and source article count.
   - **Most Active Public Figures & Organizations**: Grid of top entities ranked by total salient events.

2. **`GET /search?q={query}&mode={lexical|semantic}&topic={tag}`**:
   - **Lexical Mode**: Queries `TB_Entity` (`pg_trgm` on `canonical_name` and `aliases`) and `TB_Event` (`canonical_title`, `summary`).
   - **Semantic Position Mode**: Embeds `q` via `LocalEncoderClient.embed([q], task="SEARCH_QUERY")` (`Qwen/Qwen3-Embedding-4B`, 1024-dim MRL) and ranks `TB_Entity_Event.action_embedding` and `TB_Event.embedding` by cosine similarity.

3. **`GET /wiki/entity/{entity_id}?sort={salience|recent}&topic={tag}` (Entity Wiki Dossier Page)**:
   - **Header**: Canonical Name, Entity Type badge (`PERSON`, `GOVERNMENT_BODY`, `ORGANIZATION`, `COALITION`), Known Aliases, and an Admin `"Regenerate Bio"` button (if logged in).
   - **Main Left Column**:
     1. **Executive Overview**: Rendered `summary_bio` with clickable `[[Wiki Links]]`.
     2. **Filter & Sort Controls (HTMX)**:
        - **Sort Toggle**: `Most Defining (Salience)` (queries `VW_Entity_Salient_Career_Events ORDER BY salience_score DESC`) vs. `Chronological (Recent)` (queries `VW_Entity_Career_Ledger ORDER BY effective_date DESC`).
        - **Topic Filter Pills**: Clickable pills for every `topic_tag` associated with this entity.
     3. **Career & Stance Ledger**: Each entry displays:
        - Event Date, Linked Event Title (`/wiki/event/{event_id}`), and Citation Badge (`📰 Cited in X articles | Salience: Y`)
        - Role in Event & Color-Coded Stance Badge (`SUPPORT`=emerald, `OPPOSE`=rose, `APOLOGETIC`=amber, `DEFENSIVE`=orange, `INVESTIGATIVE`=purple, `NEUTRAL`/`PARTICIPANT`=slate)
        - `combined_action_summary` / `action_summary` (with `[[Wiki Links]]` auto-linked)
        - Blockquote verbatim `direct_quotes`
        - Primary-source article footnote links (`[Source 1]`, `[Source 2]`)
   - **Right Sidebar (Wikipedia-style Infobox)**:
     1. **Positions & Affiliations (`TB_Entity_Affiliation`)**: Role title, linked Organization (`/wiki/entity/{org_id}`), and `first_seen_date` – `last_seen_date`.
     2. **Top Coalition Allies (`VW_Entity_Coalition_Matrix` where `alignment_index > 0`)**: Linked entity name, shared events count, alignment score (`+85%`), and shared issue tags.
     3. **Frequent Opponents / Counterparts (`alignment_index < 0`)**: Linked entity name, opposing events count, and alignment score.

4. **`GET /wiki/event/{event_id}` (Event Wiki Page)**:
   - Displays `canonical_title`, `event_date`, `event_type` badge, `topic_tags`, and `summary`.
   - **Source Articles Box**: Lists all articles in `TB_Article_Event` covering this incident with publication dates and external links.
   - **Involved Actors by Stance**: Groups all `TB_Entity_Event` rows for this event into cards showing each actor's linked profile, role, stance badge, action summary, and verbatim quotes.

---

## 7. Password-Protected Admin Backend & Failure Alerting (`civic_tracker/web/admin.py`)

### Authentication
- `GET /admin/login` & `POST /admin/login`: Validates password against `settings.ADMIN_PASSWORD` using `secrets.compare_digest` and sets an HTTP-only signed cookie via `itsdangerous.TimestampSigner`.
- Dependency `require_admin(request: Request)` protects all `/admin/*` endpoints.

### Admin Dashboard Routes
1. **`GET /admin` (Control Center, Scheduler Status, & Job Telemetry)**:
   - **Scheduler Bar**: Shows current state (`Active` / `Paused`), next scheduled run timestamp (`scheduler.get_job("civic_full_cycle").next_run_time`), a `Pause / Resume` toggle (`POST /admin/scheduler/toggle`), and a `Run Full Cycle Now` button (`POST /admin/jobs/run-cycle`).
   - **Database Counters**: Total Sources, Articles (`PENDING`, `PROCESSED`, `FAILED`, `SKIPPED`), Entities (`bio_is_dirty` count), and Events.
   - **Recent Job Runs (`TB_Job_Run`)**: Status badges, timestamps, processed/failed counts, and expandable log output modal.
   - **Manual Stage Triggers**: `Crawl Active Sources`, `Process Pending Articles`, and `Synthesize Dirty Wiki Bios`.

2. **`GET /admin/sources` & `POST /admin/sources` (Source & Backfill Manager)**:
   - Add/edit/toggle `TB_Sources` (`name`, `base_url`, `sitemap_url`, `rss_url`, `is_active`).
   - **Retroactive Backfill Trigger**: Per-source form (`Backfill last [X] days` or `Since [YYYY-MM-DD]`) launching an async background backfill job logged in `TB_Job_Run`.

3. **`GET /admin/failures` (Pipeline Error Inspector & Retry Queue)**:
   - Displays all `TB_Article WHERE processing_status = 'FAILED'` with title, source URL, `scraped_at`, and full `error_log`.
   - Actions: `Retry All Failed` / `Retry Selected` (resets status to `PENDING`) or `Mark Skipped`.

4. **`GET /admin/entities/merge` & `POST /admin/entities/merge` (Manual Entity Deduplication)**:
   - Merges `source_entity_id` into `target_entity_id`: combines `canonical_name` and `aliases`, reassigns `TB_Entity_Affiliation` and `TB_Entity_Event` records (handling conflicts cleanly), deletes `source_entity_id`, and marks `target.bio_is_dirty = TRUE`.

### Automated Webhook Alerting (`civic_tracker/pipeline/alerts.py`)
- When any `TB_Job_Run` completes with `status IN ('FAILED', 'PARTIAL_FAILURE')`, send an async HTTP POST to `settings.ALERT_WEBHOOK_URL` (if set) with `{"text": "🚨 **Civic Tracker Alert**: Job ..."}` and surface a persistent alert banner in `/admin`.

---

## 8. CLI Entrypoints (`civic_tracker/cli.py`)
Add the following commands to `cli.py`:
1. `civic-tracker db-init-web`: Applies `db/schema_web.sql` (idempotent).
2. `civic-tracker synthesize-wiki [--limit 100]`: Runs Stage 3 Wiki Lead Synthesizer on dirty entities.
3. `civic-tracker run-cycle`: Runs one full chained cycle (`Crawl -> Process -> Wiki Synthesis`) from the CLI.
4. `civic-tracker serve [--host 0.0.0.0] [--port 8080] [--with-scheduler / --no-scheduler]`: Starts Uvicorn (`workers=1`) with the Public Wiki, Admin UI, and in-process `APScheduler` loop.
