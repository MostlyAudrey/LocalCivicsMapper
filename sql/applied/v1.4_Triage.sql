-- v1.4: Article triage (skip non-local / non-news before any NLP or LLM work).
-- Idempotent. Mirrored at the end of civic_tracker/civic_tracker/db/schema_web.sql.

ALTER TABLE TB_Article
    ADD COLUMN IF NOT EXISTS genre TEXT,            -- news | opinion | announcement | lifestyle | sports | ...
    ADD COLUMN IF NOT EXISTS yolo_relevance TEXT,   -- local | regional | none
    ADD COLUMN IF NOT EXISTS skip_reason TEXT,      -- why triage skipped it; 'override' = admin forced processing
    ADD COLUMN IF NOT EXISTS triaged_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_article_skipped ON TB_Article (processed_at DESC) WHERE processing_status = 'SKIPPED';
