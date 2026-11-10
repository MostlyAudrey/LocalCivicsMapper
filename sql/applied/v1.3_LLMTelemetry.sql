-- v1.3: LLM call + pipeline stage timing telemetry (no LLM timeouts any more; we measure instead).
-- Charted on /admin/metrics. Idempotent. Mirrored at the end of civic_tracker/civic_tracker/db/schema_web.sql.

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
