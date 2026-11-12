-- v1.9: Coalition alignment ignores events where either side only took part.
-- Idempotent. Mirrored as civic_tracker/civic_tracker/db/schema_web_v19.sql (loaded by db-init-web and the tests).
--
-- Before: alignment_index = (aligned - opposed) / ALL shared events (weighted). An event where one side is NEUTRAL or
-- PARTICIPANT (e.g. the candidate in their own election, while their campaign manager DEFENDS the campaign) can never
-- be aligned or opposed, so it only diluted the score: a candidate and their campaign manager read
-- +73% from one SUPPORT/SUPPORT event and one PARTICIPANT/DEFENSIVE event.
-- After: those events still count in shared_events_count and evidence_weight (being seen together is still shown),
-- but alignment_index = (aligned - opposed) / events where BOTH sides took a stance. NULL when there are none
-- (such pairs show in neither the allies nor the opponents list, as before, when the index was 0).
-- Same columns as v1.5, so CREATE OR REPLACE is safe.

CREATE OR REPLACE VIEW VW_Entity_Coalition_Matrix AS
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
          OR (a.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))) AS opposed,
        (a.stance NOT IN ('NEUTRAL', 'PARTICIPANT') AND b.stance NOT IN ('NEUTRAL', 'PARTICIPANT')) AS informative
    FROM w a JOIN w b ON a.event_id = b.event_id AND a.entity_id <> b.entity_id
),
agg AS (
    SELECT ea, eb, COUNT(*) AS shared, COUNT(*) FILTER (WHERE aligned) AS aligned_n,
           COUNT(*) FILTER (WHERE opposed) AS opposed_n, SUM(pw) AS w_total,
           COALESCE(SUM(pw) FILTER (WHERE informative), 0) AS w_informative,
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
    ROUND((agg.w_aligned - agg.w_opposed) / NULLIF(agg.w_informative, 0), 3) AS alignment_index,  -- -1 .. +1
    ARRAY(SELECT DISTINCT t FROM TB_Event ev, unnest(ev.topic_tags) t WHERE ev.id = ANY(agg.event_ids) ORDER BY t)
        AS shared_topic_tags,
    ROUND(agg.w_total, 3) AS evidence_weight
FROM agg
JOIN TB_Entity e1 ON e1.id = agg.ea
JOIN TB_Entity e2 ON e2.id = agg.eb;
