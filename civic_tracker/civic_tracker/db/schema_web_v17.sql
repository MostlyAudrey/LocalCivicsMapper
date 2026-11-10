-- v1.7: "Not duplicates" decisions from Admin -> Merge entities.
-- Idempotent. Mirrored as civic_tracker/civic_tracker/db/schema_web_v17.sql (loaded by db-init-web and the tests).
--
-- A dismissed pair is never suggested again as a possible duplicate (e.g. "State of California" vs
-- "State Bar of California", or two different congressional districts). Stored with the smaller id first.
-- Rows disappear with either entity (ON DELETE CASCADE), e.g. when one of them is later merged away.

CREATE TABLE IF NOT EXISTS TB_Entity_Not_Duplicate (
    entity_a_id  INTEGER NOT NULL REFERENCES TB_Entity(id) ON DELETE CASCADE,
    entity_b_id  INTEGER NOT NULL REFERENCES TB_Entity(id) ON DELETE CASCADE,
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_a_id, entity_b_id),
    CHECK (entity_a_id < entity_b_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_not_duplicate_b ON TB_Entity_Not_Duplicate (entity_b_id);
