-- v1.5: Source weighting, evidence types, disputes, provisional events (bias fix).
--
-- Every stance row (TB_Entity_Event) gets an effective WEIGHT, computed live in VW_Entity_Event_Weighted:
--     weight = source reliability x genre weight x evidence weight      (0 if disputed)
--   source reliability : TB_Sources.reliability, 0-1, editable in Admin -> Sources
--   genre weight       : news 1.0, announcement 0.8, opinion 0.35, unknown 0.8   (TB_Article.genre, v1.4)
--   evidence weight    : reported 1.0, self 0.8, claimed 0.25                    (TB_Entity_Event.evidence_type)
-- The coalition matrix, salience ranking and career ledger are rebuilt on top of those weights:
--   * rows below MIN weight 0.2 do not count toward alliances/opposition (one op-ed can't create an alliance)
--   * salience is scaled by the strongest evidence for each entity-event
--   * disputed rows disappear from every view
-- Idempotent. Mirrored at the end of civic_tracker/civic_tracker/db/schema_web.sql.

-- ---------------------------------------------------------------------------
-- 1. Columns
-- ---------------------------------------------------------------------------
ALTER TABLE TB_Sources
    ADD COLUMN IF NOT EXISTS reliability NUMERIC(3, 2) NOT NULL DEFAULT 1.0;
ALTER TABLE TB_Sources DROP CONSTRAINT IF EXISTS tb_sources_reliability_check;
ALTER TABLE TB_Sources ADD CONSTRAINT tb_sources_reliability_check CHECK (reliability BETWEEN 0 AND 1);

ALTER TABLE TB_Entity_Event
    ADD COLUMN IF NOT EXISTS evidence_type TEXT NOT NULL DEFAULT 'reported',
    ADD COLUMN IF NOT EXISTS claimed_by TEXT,            -- who asserted it, for evidence_type = 'claimed'
    ADD COLUMN IF NOT EXISTS disputed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS dispute_note TEXT,
    ADD COLUMN IF NOT EXISTS disputed_at TIMESTAMPTZ;
ALTER TABLE TB_Entity_Event DROP CONSTRAINT IF EXISTS tb_entity_event_evidence_type_check;
ALTER TABLE TB_Entity_Event ADD CONSTRAINT tb_entity_event_evidence_type_check
    CHECK (evidence_type IN ('reported', 'self', 'claimed'));

-- An event that so far exists only in opinion pieces (no news coverage yet).
ALTER TABLE TB_Event ADD COLUMN IF NOT EXISTS provisional BOOLEAN NOT NULL DEFAULT FALSE;

-- ---------------------------------------------------------------------------
-- 2. Initial source reliability (agreed 2026-11-08)
-- ---------------------------------------------------------------------------
UPDATE TB_Sources SET reliability = 1.0 WHERE name IN ('Davis Enterprise', 'Winters Express');
UPDATE TB_Sources SET reliability = 0.8 WHERE name IN ('Davis Vanguard', 'The California Aggie');
UPDATE TB_Sources SET reliability = 0.7 WHERE name = 'Davis Dirt';
UPDATE TB_Sources SET reliability = 0.6 WHERE name = 'The Blue Devil Hub';
UPDATE TB_Sources SET reliability = 0.3 WHERE name = 'Davisite';

-- ---------------------------------------------------------------------------
-- 3. Weighted base view
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW VW_Entity_Event_Weighted AS
SELECT
    ee.id, ee.entity_id, ee.event_id, ee.source_article_id, ee.role_in_event, ee.stance, ee.action_summary,
    ee.direct_quotes, ee.confidence, ee.evidence_type, ee.claimed_by, ee.disputed, ee.dispute_note,
    a.genre, a.published_at, a.url AS source_url, a.title AS article_title,
    s.id AS source_id, s.name AS source_name, s.reliability,
    CASE a.genre WHEN 'news' THEN 1.0 WHEN 'announcement' THEN 0.8 WHEN 'opinion' THEN 0.35 ELSE 0.8 END AS genre_weight,
    CASE ee.evidence_type WHEN 'reported' THEN 1.0 WHEN 'self' THEN 0.8 WHEN 'claimed' THEN 0.25 ELSE 1.0 END AS evidence_weight,
    CASE WHEN ee.disputed THEN 0::NUMERIC ELSE ROUND(
        s.reliability
        * CASE a.genre WHEN 'news' THEN 1.0 WHEN 'announcement' THEN 0.8 WHEN 'opinion' THEN 0.35 ELSE 0.8 END
        * CASE ee.evidence_type WHEN 'reported' THEN 1.0 WHEN 'self' THEN 0.8 WHEN 'claimed' THEN 0.25 ELSE 1.0 END,
        3) END AS weight
FROM TB_Entity_Event ee
JOIN TB_Article a ON a.id = ee.source_article_id
JOIN TB_Sources s ON s.id = a.source_id;

-- ---------------------------------------------------------------------------
-- 4. Coalition matrix: weighted, minimum-evidence alignment (same leading columns as before)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS VW_Entity_Coalition_Matrix;
CREATE VIEW VW_Entity_Coalition_Matrix AS
WITH w AS (
    -- strongest qualifying row per (entity, event); weak evidence (< 0.2) never forms alliances
    SELECT DISTINCT ON (entity_id, event_id) entity_id, event_id, stance, weight
    FROM VW_Entity_Event_Weighted
    WHERE NOT disputed AND weight >= 0.2
    ORDER BY entity_id, event_id, weight DESC, confidence DESC, id DESC
),
pairs AS (
    SELECT a.entity_id AS ea, b.entity_id AS eb, a.event_id, LEAST(a.weight, b.weight) AS pw,
        ((a.stance IN ('SUPPORT', 'DEFENSIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))
          OR (a.stance IN ('OPPOSE', 'APOLOGETIC') AND b.stance IN ('OPPOSE', 'APOLOGETIC'))
          OR (a.stance = b.stance AND a.stance NOT IN ('NEUTRAL', 'PARTICIPANT'))) AS aligned,
        ((a.stance IN ('SUPPORT', 'DEFENSIVE') AND b.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE'))
          OR (a.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))) AS opposed
    FROM w a JOIN w b ON a.event_id = b.event_id AND a.entity_id <> b.entity_id
),
agg AS (
    SELECT ea, eb, COUNT(*) AS shared, COUNT(*) FILTER (WHERE aligned) AS aligned_n,
           COUNT(*) FILTER (WHERE opposed) AS opposed_n, SUM(pw) AS w_total,
           COALESCE(SUM(pw) FILTER (WHERE aligned), 0) AS w_aligned,
           COALESCE(SUM(pw) FILTER (WHERE opposed), 0) AS w_opposed,
           ARRAY_AGG(event_id) AS event_ids
    FROM pairs GROUP BY ea, eb
)
SELECT
    e1.id AS entity_a_id, e1.canonical_name AS entity_a_name, e1.entity_type AS entity_a_type,
    e2.id AS entity_b_id, e2.canonical_name AS entity_b_name, e2.entity_type AS entity_b_type,
    agg.shared AS shared_events_count,
    agg.aligned_n AS aligned_stance_count,
    agg.opposed_n AS opposing_stance_count,
    ROUND((agg.w_aligned - agg.w_opposed) / NULLIF(agg.w_total, 0), 3) AS alignment_index,  -- -1 .. +1, weighted
    ARRAY(SELECT DISTINCT t FROM TB_Event ev, unnest(ev.topic_tags) t WHERE ev.id = ANY(agg.event_ids) ORDER BY t)
        AS shared_topic_tags,
    ROUND(agg.w_total, 3) AS evidence_weight
FROM agg
JOIN TB_Entity e1 ON e1.id = agg.ea
JOIN TB_Entity e2 ON e2.id = agg.eb;

-- ---------------------------------------------------------------------------
-- 5. Salience ledger: spec formula x evidence strength; disputed rows excluded
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS VW_Entity_Salient_Career_Events;
CREATE VIEW VW_Entity_Salient_Career_Events AS
WITH live AS (
    SELECT * FROM VW_Entity_Event_Weighted WHERE NOT disputed
),
event_gravity AS (
    SELECT ev.id AS event_id,
           COUNT(DISTINCT ae.article_id) AS total_event_articles,
           COUNT(DISTINCT l.entity_id) AS total_event_actors
    FROM TB_Event ev
    LEFT JOIN TB_Article_Event ae ON ae.event_id = ev.id
    LEFT JOIN live l ON l.event_id = ev.id
    GROUP BY ev.id
),
entity_event_rollup AS (
    SELECT
        l.entity_id, l.event_id,
        COUNT(DISTINCT l.source_article_id) AS entity_article_citations,
        (ARRAY_AGG(l.stance ORDER BY l.weight DESC,
            CASE WHEN l.stance IN ('SUPPORT', 'OPPOSE', 'APOLOGETIC', 'DEFENSIVE', 'INVESTIGATIVE') THEN 1 ELSE 2 END,
            cardinality(l.direct_quotes) DESC, l.confidence DESC))[1] AS primary_stance,
        (ARRAY_AGG(l.role_in_event ORDER BY l.weight DESC, length(l.role_in_event) DESC))[1] AS primary_role_in_event,
        STRING_AGG(DISTINCT l.action_summary, ' | ') AS combined_action_summary,
        ARRAY_REMOVE(ARRAY_AGG(DISTINCT q), NULL) AS all_direct_quotes,
        ARRAY_AGG(DISTINCT l.source_article_id) AS source_article_ids,
        MAX(l.weight) AS max_weight,
        BOOL_OR(l.genre = 'opinion' OR l.evidence_type = 'claimed') AS has_commentary,
        ARRAY_AGG(DISTINCT l.source_name) AS source_names
    FROM live l
    LEFT JOIN LATERAL unnest(l.direct_quotes) AS q ON TRUE
    GROUP BY l.entity_id, l.event_id
),
scored_events AS (
    SELECT
        r.entity_id, e.canonical_name, ev.id AS event_id, ev.canonical_title AS event_title, ev.event_type,
        ev.event_date, ev.topic_tags, COALESCE(ev.topic_tags[1], 'general') AS primary_topic,
        r.entity_article_citations, eg.total_event_articles, eg.total_event_actors,
        r.primary_role_in_event, r.primary_stance, r.combined_action_summary, r.all_direct_quotes,
        r.source_article_ids,
        ROUND(((
            (3.0 * LN(1 + r.entity_article_citations)) +
            (1.5 * LN(1 + eg.total_event_articles)) +
            (1.2 * LN(1 + eg.total_event_actors)) +
            (CASE WHEN r.primary_stance IN ('SUPPORT', 'OPPOSE', 'APOLOGETIC', 'DEFENSIVE', 'INVESTIGATIVE')
                  THEN 2.0 ELSE 0.0 END) +
            (CASE WHEN COALESCE(cardinality(r.all_direct_quotes), 0) > 0 THEN 1.5 ELSE 0.0 END) +
            (CASE WHEN ev.event_type IN ('LEGISLATIVE_VOTE', 'POLICY_ANNOUNCEMENT', 'PUBLIC_CONTROVERSY', 'LEGAL_ACTION')
                  THEN 1.0 ELSE 0.0 END) +
            (1.5 / (1.0 + GREATEST(0, (CURRENT_DATE - COALESCE(ev.event_date, CURRENT_DATE)))::NUMERIC / 365.0))
        ) * (0.25 + 0.75 * LEAST(r.max_weight, 1.0)))::NUMERIC, 3) AS salience_score,
        r.max_weight AS evidence_weight,
        r.has_commentary,
        (r.max_weight < 0.2) AS commentary_only,
        ev.provisional,
        r.source_names
    FROM entity_event_rollup r
    JOIN TB_Entity e ON e.id = r.entity_id
    JOIN TB_Event ev ON ev.id = r.event_id
    JOIN event_gravity eg ON eg.event_id = ev.id
)
SELECT *,
    ROW_NUMBER() OVER (PARTITION BY entity_id, primary_topic
                       ORDER BY salience_score DESC, event_date DESC NULLS LAST) AS topic_rank
FROM scored_events;

-- ---------------------------------------------------------------------------
-- 6. Chronological ledger: + provenance and weight, disputed rows excluded
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS VW_Entity_Career_Ledger;
CREATE VIEW VW_Entity_Career_Ledger AS
SELECT
    e.id AS entity_id, e.canonical_name, e.entity_type,
    ev.id AS event_id,
    COALESCE(ev.event_date, w.published_at::DATE) AS effective_date,
    ev.canonical_title AS event_title, ev.event_type, ev.topic_tags,
    w.role_in_event, w.stance, w.action_summary, w.direct_quotes,
    w.source_url, w.article_title,
    w.id AS entity_event_id, w.source_article_id, w.source_name, w.genre, w.evidence_type, w.claimed_by,
    w.weight, ev.provisional
FROM TB_Entity e
JOIN VW_Entity_Event_Weighted w ON w.entity_id = e.id AND NOT w.disputed
JOIN TB_Event ev ON ev.id = w.event_id
ORDER BY e.id, effective_date DESC;
