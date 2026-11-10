-- v1.6: Official records (city council staff reports from CivicClerk: Woodland, Winters).
-- Idempotent. Mirrored as civic_tracker/civic_tracker/db/schema_web_v16.sql (loaded by db-init-web and the tests).
--
-- New genre value 'official_record' (TB_Article.genre is free TEXT since v1.4, no CHECK to change):
--   * set by the crawler at insert time together with triaged_at, so the LLM triage is skipped;
--   * genre weight 1.0 (same as news) in the weighted view below.

-- 1. Record metadata (platform, tenant, body, meeting / item ids, staff-report URL, pages, attachment names)
ALTER TABLE TB_Article ADD COLUMN IF NOT EXISTS doc_meta JSONB;

CREATE INDEX IF NOT EXISTS idx_article_official_record
    ON TB_Article (published_at DESC) WHERE genre = 'official_record';

-- 2. Weighted base view (v1.5): official_record weighs 1.0. Same columns, so CREATE OR REPLACE is enough.
CREATE OR REPLACE VIEW VW_Entity_Event_Weighted AS
SELECT
    ee.id, ee.entity_id, ee.event_id, ee.source_article_id, ee.role_in_event, ee.stance, ee.action_summary,
    ee.direct_quotes, ee.confidence, ee.evidence_type, ee.claimed_by, ee.disputed, ee.dispute_note,
    a.genre, a.published_at, a.url AS source_url, a.title AS article_title,
    s.id AS source_id, s.name AS source_name, s.reliability,
    CASE a.genre WHEN 'news' THEN 1.0 WHEN 'official_record' THEN 1.0 WHEN 'announcement' THEN 0.8
                 WHEN 'opinion' THEN 0.35 ELSE 0.8 END AS genre_weight,
    CASE ee.evidence_type WHEN 'reported' THEN 1.0 WHEN 'self' THEN 0.8 WHEN 'claimed' THEN 0.25 ELSE 1.0 END AS evidence_weight,
    CASE WHEN ee.disputed THEN 0::NUMERIC ELSE ROUND(
        s.reliability
        * CASE a.genre WHEN 'news' THEN 1.0 WHEN 'official_record' THEN 1.0 WHEN 'announcement' THEN 0.8
                       WHEN 'opinion' THEN 0.35 ELSE 0.8 END
        * CASE ee.evidence_type WHEN 'reported' THEN 1.0 WHEN 'self' THEN 0.8 WHEN 'claimed' THEN 0.25 ELSE 1.0 END,
        3) END AS weight
FROM TB_Entity_Event ee
JOIN TB_Article a ON a.id = ee.source_article_id
JOIN TB_Sources s ON s.id = a.source_id;
