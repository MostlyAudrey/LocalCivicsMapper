-- v1.8: Measures and roll-call votes.
-- Idempotent. Mirrored as civic_tracker/civic_tracker/db/schema_web_v18.sql (loaded by db-init-web and the tests).
--
-- A MEASURE is the thing a body acts on: an ordinance, resolution, contract, budget, land-use approval, appointment,
-- policy... (one row per measure per body; identified by its number when it has one, e.g. "ORD 2654").
-- A MOTION is one vote on a measure at one meeting (introduction, adoption, amendment, continuance...).
-- A VOTE is one member's ballot on a motion. Roll calls come ONLY from official minutes (user decision
-- 2026-11-10: news reports of votes do not count); news coverage is linked to measures through events.

CREATE TABLE IF NOT EXISTS TB_Measure (
    id               SERIAL PRIMARY KEY,
    body_entity_id   INTEGER REFERENCES TB_Entity(id) ON DELETE SET NULL,   -- e.g. Davis City Council
    jurisdiction     TEXT,                                                  -- Davis / Woodland / ... / Yolo County
    measure_type     TEXT NOT NULL DEFAULT 'OTHER' CHECK (measure_type IN
                       ('ORDINANCE', 'RESOLUTION', 'BUDGET', 'CONTRACT', 'LAND_USE', 'APPOINTMENT', 'POLICY',
                        'BALLOT_MEASURE', 'OTHER')),
    measure_number   TEXT,                       -- as written, e.g. "Ordinance No. 2654"
    number_key       TEXT,                       -- normalized for matching, e.g. "ORD-2654"
    title            TEXT NOT NULL,
    summary          TEXT,
    status           TEXT,                       -- latest outcome: PASSED / FAILED / CONTINUED / TABLED / ...
    first_action_date DATE,
    last_action_date  DATE,
    topic_tags       TEXT[] NOT NULL DEFAULT '{}',
    title_embedding  vector(1024),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_measure_number
    ON TB_Measure (COALESCE(jurisdiction, ''), number_key) WHERE number_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_measure_body ON TB_Measure (body_entity_id, last_action_date DESC);
CREATE INDEX IF NOT EXISTS idx_measure_title_trgm ON TB_Measure USING gin (title gin_trgm_ops);

-- events (from news or records) that are about a measure; how = 'explicit' (the article names it) | 'vote'
CREATE TABLE IF NOT EXISTS TB_Measure_Event (
    measure_id  INTEGER NOT NULL REFERENCES TB_Measure(id) ON DELETE CASCADE,
    event_id    INTEGER NOT NULL REFERENCES TB_Event(id) ON DELETE CASCADE,
    how         TEXT NOT NULL DEFAULT 'explicit',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (measure_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_measure_event_event ON TB_Measure_Event (event_id);

CREATE TABLE IF NOT EXISTS TB_Vote_Motion (
    id                SERIAL PRIMARY KEY,
    measure_id        INTEGER NOT NULL REFERENCES TB_Measure(id) ON DELETE CASCADE,
    event_id          INTEGER REFERENCES TB_Event(id) ON DELETE SET NULL,
    body_entity_id    INTEGER REFERENCES TB_Entity(id) ON DELETE SET NULL,
    source_article_id INTEGER NOT NULL REFERENCES TB_Article(id) ON DELETE CASCADE,   -- the minutes
    vote_date         DATE,
    motion_text       TEXT NOT NULL,
    outcome           TEXT CHECK (outcome IN ('PASSED', 'FAILED', 'CONTINUED', 'TABLED', 'WITHDRAWN', 'OTHER')),
    tally             TEXT,                     -- as recorded, e.g. "4-1" or "5-0-0-0"
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_article_id, measure_id, motion_text)
);
CREATE INDEX IF NOT EXISTS idx_vote_motion_measure ON TB_Vote_Motion (measure_id, vote_date);

CREATE TABLE IF NOT EXISTS TB_Vote (
    id         SERIAL PRIMARY KEY,
    motion_id  INTEGER NOT NULL REFERENCES TB_Vote_Motion(id) ON DELETE CASCADE,
    entity_id  INTEGER REFERENCES TB_Entity(id) ON DELETE SET NULL,   -- NULL when the name couldn't be matched
    raw_name   TEXT NOT NULL,                                          -- as written in the minutes
    vote       TEXT NOT NULL CHECK (vote IN ('AYE', 'NO', 'ABSTAIN', 'ABSENT', 'RECUSED')),
    UNIQUE (motion_id, raw_name)
);
CREATE INDEX IF NOT EXISTS idx_vote_entity ON TB_Vote (entity_id);

-- one row per ballot with the motion's majority, for voting-record analysis
CREATE OR REPLACE VIEW VW_Voting_Record AS
WITH majority AS (
    SELECT motion_id,
           CASE WHEN COUNT(*) FILTER (WHERE vote = 'AYE') > COUNT(*) FILTER (WHERE vote = 'NO') THEN 'AYE'
                WHEN COUNT(*) FILTER (WHERE vote = 'NO') > COUNT(*) FILTER (WHERE vote = 'AYE') THEN 'NO'
           END AS majority_vote
    FROM TB_Vote GROUP BY motion_id
)
SELECT v.id AS vote_id, v.entity_id, e.canonical_name AS member_name, v.raw_name, v.vote,
       mo.id AS motion_id, mo.motion_text, mo.outcome, mo.tally, mo.vote_date, mo.source_article_id,
       me.id AS measure_id, me.title AS measure_title, me.measure_type, me.measure_number, me.jurisdiction,
       me.topic_tags, b.id AS body_id, b.canonical_name AS body_name,
       mj.majority_vote,
       (v.vote IN ('AYE', 'NO') AND mj.majority_vote IS NOT NULL AND v.vote <> mj.majority_vote) AS dissent
FROM TB_Vote v
JOIN TB_Vote_Motion mo ON mo.id = v.motion_id
JOIN TB_Measure me ON me.id = mo.measure_id
LEFT JOIN TB_Entity e ON e.id = v.entity_id
LEFT JOIN TB_Entity b ON b.id = COALESCE(mo.body_entity_id, me.body_entity_id)
LEFT JOIN majority mj ON mj.motion_id = v.motion_id;
