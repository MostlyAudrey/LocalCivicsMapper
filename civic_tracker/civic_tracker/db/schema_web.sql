-- Mirror of /root/sql/unapplied/v1.2_WebUI.sql. Applied by `civic-tracker db-init-web` (and the test suite).

-- ---------------------------------------------------------------------------
-- 1. Stage 3 dirty flag (SPEC §3.1)
-- ---------------------------------------------------------------------------
ALTER TABLE TB_Entity
    ADD COLUMN IF NOT EXISTS bio_is_dirty BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS bio_updated_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_entity_bio_dirty ON TB_Entity(bio_is_dirty) WHERE bio_is_dirty = TRUE;

-- ---------------------------------------------------------------------------
-- 2. Job telemetry (SPEC §3.2 + §5.1 CANCELLED)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS TB_Job_Run (
    id SERIAL PRIMARY KEY,
    job_type TEXT NOT NULL CHECK (job_type IN ('FULL_CYCLE', 'CRAWL', 'BACKFILL', 'PROCESS_PIPELINE', 'WIKI_SYNTHESIS')),
    source_id INTEGER REFERENCES TB_Sources(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    items_processed INTEGER DEFAULT 0,
    items_failed INTEGER DEFAULT 0,
    log_output TEXT DEFAULT '',
    error_summary TEXT
);
ALTER TABLE TB_Job_Run DROP CONSTRAINT IF EXISTS tb_job_run_status_check;
ALTER TABLE TB_Job_Run ADD CONSTRAINT tb_job_run_status_check
    CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL_FAILURE', 'FAILED', 'CANCELLED'));
ALTER TABLE TB_Job_Run ADD COLUMN IF NOT EXISTS alert_acknowledged BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_job_run_started ON TB_Job_Run(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_run_status ON TB_Job_Run(status);

-- ---------------------------------------------------------------------------
-- 3. Salience-ranked career ledger (SPEC §3.3, verbatim)
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- 4. Scheduler state (SPEC §5.1) + processor pause switch
--    is_enabled / interval_hours drive the CRAWL schedule (crawler only queues articles).
--    processor_enabled pauses/resumes the continuous article processor.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS TB_Scheduler_State (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    interval_hours FLOAT NOT NULL DEFAULT 6.0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE TB_Scheduler_State ADD COLUMN IF NOT EXISTS processor_enabled BOOLEAN NOT NULL DEFAULT TRUE;
INSERT INTO TB_Scheduler_State (id, is_enabled, interval_hours)
VALUES (1, TRUE, 6.0)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 5. Article queue: atomic claims by the continuous processor
-- ---------------------------------------------------------------------------
ALTER TABLE TB_Article DROP CONSTRAINT IF EXISTS tb_article_processing_status_check;
ALTER TABLE TB_Article ADD CONSTRAINT tb_article_processing_status_check
    CHECK (processing_status IN ('PENDING', 'STAGE1_DONE', 'IN_PROGRESS', 'PROCESSED', 'FAILED', 'SKIPPED'));
ALTER TABLE TB_Article
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
-- Newest-first queue scan
CREATE INDEX IF NOT EXISTS idx_article_queue
    ON TB_Article (published_at DESC NULLS LAST, id DESC)
    WHERE processing_status IN ('PENDING', 'STAGE1_DONE');

-- ---------------------------------------------------------------------------
-- 6. Web <-> worker control plane (replaces the spec's in-process manager)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS TB_Worker_Command (
    id SERIAL PRIMARY KEY,
    command TEXT NOT NULL CHECK (command IN (
        'CRAWL_NOW',          -- params: {"source_id": int|null}
        'BACKFILL',           -- params: {"source_id": int, "backfill_days": int|null, "since": "YYYY-MM-DD"|null, "limit": int|null}
        'STOP_CRAWL',         -- cancel the running crawl/backfill
        'CANCEL_PROCESSING',  -- abort in-flight articles now (they return to the queue)
        'SYNTHESIZE_BIOS',    -- params: {"limit": int}
        'REGENERATE_BIO'      -- params: {"entity_id": int}  (bypasses eligibility threshold)
    )),
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED', 'CANCELLED')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_worker_command_queued ON TB_Worker_Command(id) WHERE status = 'QUEUED';

CREATE TABLE IF NOT EXISTS TB_Worker_Status (
    id TEXT PRIMARY KEY DEFAULT 'main',
    pid INTEGER,
    hostname TEXT,
    started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    crawler_state TEXT DEFAULT 'IDLE',      -- IDLE | RUNNING | STOPPING
    crawler_detail TEXT DEFAULT '',
    crawler_job_run_id INTEGER,
    processor_state TEXT DEFAULT 'IDLE',    -- IDLE | RUNNING | PAUSED | SYNTHESIZING
    processor_detail TEXT DEFAULT '',
    slots JSONB DEFAULT '[]'::jsonb,        -- [{"slot":0,"article_id":..,"title":..,"stage":..,"since":..}]
    next_crawl_at TIMESTAMPTZ,
    last_crawl_finished_at TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- 7. Website settings (site-wide viewer password hash, changeable in admin)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS TB_Web_Settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- v1.3 (mirror of sql/unapplied/v1.3_LLMTelemetry.sql)
-- ============================================================================

-- One row per LLM request (Stage 2 extraction, Stage 3 bio), success or failure.
CREATE TABLE IF NOT EXISTS TB_LLM_Call (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('STAGE2_EXTRACT', 'STAGE3_BIO')),
    article_id INTEGER REFERENCES TB_Article(id) ON DELETE SET NULL,
    entity_id INTEGER REFERENCES TB_Entity(id) ON DELETE SET NULL,
    model TEXT,
    thinking BOOLEAN,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER NOT NULL,
    concurrent_calls INTEGER,          -- LLM calls in flight from this worker when this one started (incl. itself)
    input_chars INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,         -- includes reasoning tokens
    reasoning_tokens INTEGER,
    finish_reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('OK', 'ERROR', 'TRUNCATED', 'CANCELLED')),
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_call_started ON TB_LLM_Call(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_kind ON TB_LLM_Call(kind, started_at DESC);

-- Per-article stage timings (milliseconds) for the latest processing attempt.
ALTER TABLE TB_Article
    ADD COLUMN IF NOT EXISTS stage1_ms INTEGER,
    ADD COLUMN IF NOT EXISTS llm_ms INTEGER,
    ADD COLUMN IF NOT EXISTS resolve_ms INTEGER;

-- Which backend served each LLM request, and what it cost (0 for the local server).
ALTER TABLE TB_LLM_Call
    ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'local',
    ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_llm_call_provider ON TB_LLM_Call(provider, started_at DESC);

-- API burst mode: worker commands, job type, live status.
ALTER TABLE TB_Worker_Command DROP CONSTRAINT IF EXISTS tb_worker_command_command_check;
ALTER TABLE TB_Worker_Command ADD CONSTRAINT tb_worker_command_command_check CHECK (command IN (
    'CRAWL_NOW', 'BACKFILL', 'STOP_CRAWL', 'CANCEL_PROCESSING', 'SYNTHESIZE_BIOS', 'REGENERATE_BIO',
    'BURST',       -- params: {"limit": int, "max_spend_usd": float, "concurrency": int}
    'STOP_BURST'
));
ALTER TABLE TB_Job_Run DROP CONSTRAINT IF EXISTS tb_job_run_job_type_check;
ALTER TABLE TB_Job_Run ADD CONSTRAINT tb_job_run_job_type_check CHECK (job_type IN (
    'FULL_CYCLE', 'CRAWL', 'BACKFILL', 'PROCESS_PIPELINE', 'WIKI_SYNTHESIS', 'API_BURST'));
ALTER TABLE TB_Worker_Status ADD COLUMN IF NOT EXISTS burst JSONB DEFAULT '{}'::jsonb;

-- ============================================================================
-- v1.4 (mirror of sql/unapplied/v1.4_Triage.sql)
-- ============================================================================
ALTER TABLE TB_Article
    ADD COLUMN IF NOT EXISTS genre TEXT,
    ADD COLUMN IF NOT EXISTS yolo_relevance TEXT,
    ADD COLUMN IF NOT EXISTS skip_reason TEXT,
    ADD COLUMN IF NOT EXISTS triaged_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_article_skipped ON TB_Article (processed_at DESC) WHERE processing_status = 'SKIPPED';

-- v1.5 (source weighting) lives in schema_web_v15.sql (exact copy of sql/unapplied/v1.5_SourceWeighting.sql);
-- `civic-tracker db-init-web` and the test suite apply it right after this file.
