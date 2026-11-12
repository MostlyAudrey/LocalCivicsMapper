-- v2.0: Event sides. Idempotent. Mirrored as civic_tracker/civic_tracker/db/schema_web_v20.sql (db-init-web, tests).
-- Plan: PLAN_STANCE_SIDES.md.
--
-- A stance (SUPPORT/OPPOSE/...) is aimed at "the event's central action", so in a multi-sided event (an election,
-- a lawsuit, a controversy) two people who each SUPPORT their own candidate looked like allies. An event can now
-- list its SIDES ("Candidate A campaign" / "Candidate B campaign", "For Measure J" / "Against Measure J") and each
-- stance row says which side(s) it is on. An endorser of two candidates in a multi-seat race is on both sides
-- (user decision 2026-11-12: shared endorsers count), hence the link table rather than a side_id column.
--
-- Coalition alignment per shared event: when BOTH entities have sides there, same side (overlapping side sets) =
-- aligned and no overlap = opposed, whatever their stances; otherwise the v1.9 stance rules apply.

CREATE TABLE IF NOT EXISTS TB_Event_Side (
    id                SERIAL PRIMARY KEY,
    event_id          INTEGER NOT NULL REFERENCES TB_Event(id) ON DELETE CASCADE,
    label             TEXT NOT NULL,                 -- "Candidate A campaign", "For Measure J", "Aye: <motion>"
    label_key         TEXT NOT NULL,                 -- normalized label (sides.side_key): merges wording variants
    side_kind         TEXT NOT NULL DEFAULT 'other'
                        CHECK (side_kind IN ('candidate', 'position', 'party', 'vote', 'other')),
    source            TEXT NOT NULL DEFAULT 'extraction'
                        CHECK (source IN ('extraction', 'roll_call', 'backfill', 'manual')),
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (event_id, label_key)
);
CREATE INDEX IF NOT EXISTS idx_event_side_event ON TB_Event_Side(event_id);

CREATE TABLE IF NOT EXISTS TB_Entity_Event_Side (
    entity_event_id   INTEGER NOT NULL REFERENCES TB_Entity_Event(id) ON DELETE CASCADE,
    side_id           INTEGER NOT NULL REFERENCES TB_Event_Side(id) ON DELETE CASCADE,
    PRIMARY KEY (entity_event_id, side_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_event_side_side ON TB_Entity_Event_Side(side_id);

-- when an LLM last decided this event's sides (an extraction with the v2.0 prompt, or the backfill), contested or
-- not: the backfill skips checked events so it never pays twice for the same answer
ALTER TABLE TB_Event ADD COLUMN IF NOT EXISTS sides_checked_at TIMESTAMPTZ;

-- the backfill job (worker command + LLM telemetry kind)
ALTER TABLE TB_Worker_Command DROP CONSTRAINT IF EXISTS tb_worker_command_command_check;
ALTER TABLE TB_Worker_Command ADD CONSTRAINT tb_worker_command_command_check CHECK (command IN (
    'CRAWL_NOW', 'BACKFILL', 'STOP_CRAWL', 'CANCEL_PROCESSING', 'SYNTHESIZE_BIOS', 'REGENERATE_BIO',
    'BURST', 'STOP_BURST',
    'ASSIGN_SIDES'   -- params: {"limit": int, "max_spend_usd": float, "event_id": int}
));
ALTER TABLE TB_LLM_Call DROP CONSTRAINT IF EXISTS tb_llm_call_kind_check;
ALTER TABLE TB_LLM_Call ADD CONSTRAINT tb_llm_call_kind_check CHECK (kind IN ('STAGE2_EXTRACT', 'STAGE3_BIO', 'ASSIGN_SIDES'));

-- Coalition matrix: v1.9 columns unchanged (in order), two counts appended.
CREATE OR REPLACE VIEW VW_Entity_Coalition_Matrix AS
WITH w AS (
    -- strongest qualifying row per (entity, event); weak evidence (< 0.2) never forms alliances
    SELECT DISTINCT ON (entity_id, event_id) entity_id, event_id, stance, weight
    FROM VW_Entity_Event_Weighted
    WHERE NOT disputed AND weight >= 0.2
    ORDER BY entity_id, event_id, weight DESC, confidence DESC, id DESC
),
s AS (
    -- every side the entity is on in the event, from any of its non-disputed rows
    SELECT ee.entity_id, ee.event_id, ARRAY_AGG(DISTINCT l.side_id) AS sides
    FROM TB_Entity_Event ee JOIN TB_Entity_Event_Side l ON l.entity_event_id = ee.id
    WHERE NOT ee.disputed
    GROUP BY ee.entity_id, ee.event_id
),
raw AS (
    SELECT a.entity_id AS ea, b.entity_id AS eb, a.event_id, LEAST(a.weight, b.weight) AS pw,
           (sa.sides IS NOT NULL AND sb.sides IS NOT NULL) AS sided,
           COALESCE(sa.sides && sb.sides, FALSE) AS same_side,
        ((a.stance IN ('SUPPORT', 'DEFENSIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))
          OR (a.stance IN ('OPPOSE', 'APOLOGETIC') AND b.stance IN ('OPPOSE', 'APOLOGETIC'))
          OR (a.stance = b.stance AND a.stance NOT IN ('NEUTRAL', 'PARTICIPANT'))) AS stance_aligned,
        ((a.stance IN ('SUPPORT', 'DEFENSIVE') AND b.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE'))
          OR (a.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))) AS stance_opposed,
        (a.stance NOT IN ('NEUTRAL', 'PARTICIPANT') AND b.stance NOT IN ('NEUTRAL', 'PARTICIPANT')) AS stance_informative
    FROM w a JOIN w b ON a.event_id = b.event_id AND a.entity_id <> b.entity_id
    LEFT JOIN s sa ON sa.entity_id = a.entity_id AND sa.event_id = a.event_id
    LEFT JOIN s sb ON sb.entity_id = b.entity_id AND sb.event_id = b.event_id
),
pairs AS (
    SELECT ea, eb, event_id, pw, sided, same_side,
           CASE WHEN sided THEN same_side ELSE stance_aligned END AS aligned,
           CASE WHEN sided THEN NOT same_side ELSE stance_opposed END AS opposed,
           (sided OR stance_informative) AS informative
    FROM raw
),
agg AS (
    SELECT ea, eb, COUNT(*) AS shared, COUNT(*) FILTER (WHERE aligned) AS aligned_n,
           COUNT(*) FILTER (WHERE opposed) AS opposed_n, SUM(pw) AS w_total,
           COALESCE(SUM(pw) FILTER (WHERE informative), 0) AS w_informative,
           COALESCE(SUM(pw) FILTER (WHERE aligned), 0) AS w_aligned,
           COALESCE(SUM(pw) FILTER (WHERE opposed), 0) AS w_opposed,
           COUNT(*) FILTER (WHERE sided AND same_side) AS same_side_n,
           COUNT(*) FILTER (WHERE sided AND NOT same_side) AS opposite_side_n,
           ARRAY_AGG(event_id) AS event_ids
    FROM pairs GROUP BY ea, eb
)
SELECT
    e1.id AS entity_a_id, e1.canonical_name AS entity_a_name, e1.entity_type AS entity_a_type,
    e2.id AS entity_b_id, e2.canonical_name AS entity_b_name, e2.entity_type AS entity_b_type,
    agg.shared AS shared_events_count,
    agg.aligned_n AS aligned_stance_count,
    agg.opposed_n AS opposing_stance_count,
    ROUND((agg.w_aligned - agg.w_opposed) / NULLIF(agg.w_informative, 0), 3) AS alignment_index,  -- -1 .. +1
    ARRAY(SELECT DISTINCT t FROM TB_Event ev, unnest(ev.topic_tags) t WHERE ev.id = ANY(agg.event_ids) ORDER BY t)
        AS shared_topic_tags,
    ROUND(agg.w_total, 3) AS evidence_weight,
    agg.same_side_n AS same_side_count,
    agg.opposite_side_n AS opposite_side_count
FROM agg
JOIN TB_Entity e1 ON e1.id = agg.ea
JOIN TB_Entity e2 ON e2.id = agg.eb;
