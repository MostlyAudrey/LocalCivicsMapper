CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. Local News Sources
CREATE TABLE IF NOT EXISTS TB_Sources (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL UNIQUE,
    sitemap_url TEXT,
    rss_url TEXT,
    scrape_config JSONB DEFAULT '{}'::jsonb,
    last_crawled_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Scraped Articles
CREATE TABLE IF NOT EXISTS TB_Article (
    id SERIAL PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES TB_Sources(id) ON DELETE CASCADE,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT,
    published_at TIMESTAMPTZ,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    full_text TEXT NOT NULL,
    resolved_text TEXT,               -- Pronoun-resolved text from fastcoref
    evidence_bundle TEXT,             -- Condensed entity-bearing sentences from Stage 1
    content_hash TEXT NOT NULL,       -- SHA256 of full_text
    embedding vector(1024),           -- Qwen3-Embedding-4B (1024-dim MRL) of title + lead
    processing_status TEXT DEFAULT 'PENDING'
        CHECK (processing_status IN ('PENDING', 'STAGE1_DONE', 'PROCESSED', 'FAILED', 'SKIPPED')),
    error_log TEXT
);
CREATE INDEX IF NOT EXISTS idx_article_status ON TB_Article(processing_status);
CREATE INDEX IF NOT EXISTS idx_article_published ON TB_Article(published_at);
CREATE INDEX IF NOT EXISTS idx_article_embedding ON TB_Article USING hnsw (embedding vector_cosine_ops);

-- 3. Entities (People, Organizations, Government Bodies, Coalitions)
CREATE TABLE IF NOT EXISTS TB_Entity (
    id SERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL
        CHECK (entity_type IN ('PERSON', 'ORGANIZATION', 'GOVERNMENT_BODY', 'COALITION')),
    aliases TEXT[] DEFAULT '{}',
    summary_bio TEXT,
    context_embedding vector(1024),   -- Qwen3-Embedding-4B of Name + Type + Primary Role/Org
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(canonical_name, entity_type)
);
CREATE INDEX IF NOT EXISTS idx_entity_name_trgm ON TB_Entity USING gin (canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entity_context_embed ON TB_Entity USING hnsw (context_embedding vector_cosine_ops);

-- 4. Time-Bounded Career & Organizational Affiliations
CREATE TABLE IF NOT EXISTS TB_Entity_Affiliation (
    id SERIAL PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES TB_Entity(id) ON DELETE CASCADE,
    organization_id INTEGER REFERENCES TB_Entity(id) ON DELETE SET NULL,
    role_title TEXT NOT NULL,         -- e.g., "Mayor", "City Manager", "Police Officer"
    first_seen_date TIMESTAMPTZ NOT NULL,
    last_seen_date TIMESTAMPTZ NOT NULL,
    source_article_id INTEGER REFERENCES TB_Article(id) ON DELETE SET NULL,
    UNIQUE NULLS NOT DISTINCT (entity_id, organization_id, role_title)
);
CREATE INDEX IF NOT EXISTS idx_affiliation_entity ON TB_Entity_Affiliation(entity_id);

-- 5. Real-World Civic Events / Decisions / Controversies
CREATE TABLE IF NOT EXISTS TB_Event (
    id SERIAL PRIMARY KEY,
    canonical_title TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK (event_type IN (
            'LEGISLATIVE_VOTE', 'POLICY_ANNOUNCEMENT', 'PUBLIC_CONTROVERSY',
            'LAW_ENFORCEMENT_ACTION', 'MEETING', 'ELECTION', 'LEGAL_ACTION', 'OTHER'
        )),
    event_date DATE,
    summary TEXT NOT NULL,
    topic_tags TEXT[] DEFAULT '{}',   -- Lowercase normalized tags, e.g., ['immigration', 'policing']
    embedding vector(1024),           -- Qwen3-Embedding-4B (EVENT_DEDUP task)
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_event_date ON TB_Event(event_date);
CREATE INDEX IF NOT EXISTS idx_event_tags ON TB_Event USING gin (topic_tags);
CREATE INDEX IF NOT EXISTS idx_event_embedding ON TB_Event USING hnsw (embedding vector_cosine_ops);

-- 6. Many-to-Many Join: Articles <-> Events
CREATE TABLE IF NOT EXISTS TB_Article_Event (
    article_id INTEGER NOT NULL REFERENCES TB_Article(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES TB_Event(id) ON DELETE CASCADE,
    PRIMARY KEY (article_id, event_id)
);

-- 7. Many-to-Many Join: Entities <-> Events (Involvement, Stance, Quotes)
CREATE TABLE IF NOT EXISTS TB_Entity_Event (
    id SERIAL PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES TB_Entity(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES TB_Event(id) ON DELETE CASCADE,
    source_article_id INTEGER NOT NULL REFERENCES TB_Article(id) ON DELETE CASCADE,
    role_in_event TEXT NOT NULL,      -- Specific connection or role in the event
    stance TEXT NOT NULL
        CHECK (stance IN ('SUPPORT', 'OPPOSE', 'NEUTRAL', 'APOLOGETIC', 'DEFENSIVE', 'INVESTIGATIVE', 'PARTICIPANT')),
    action_summary TEXT NOT NULL,     -- 1-2 sentence synthesis of actions/statements
    direct_quotes TEXT[] DEFAULT '{}',-- Verbatim quotes pulled from the article
    action_embedding vector(1024),    -- Qwen3-Embedding-4B (SEARCH_DOCUMENT task) of action + quotes
    confidence FLOAT DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(entity_id, event_id, source_article_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_event_entity ON TB_Entity_Event(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_event_event ON TB_Entity_Event(event_id);
CREATE INDEX IF NOT EXISTS idx_entity_event_action_embed ON TB_Entity_Event USING hnsw (action_embedding vector_cosine_ops);

-- ============================================================================
-- ANALYTICAL VIEWS FOR COALITION MAPPING & CAREER TRACKING
-- ============================================================================

-- View 1: Pairwise Coalition Alignment & Co-Occurrence Network
-- Aggregates shared events between any two entities and calculates alignment scores
CREATE OR REPLACE VIEW VW_Entity_Coalition_Matrix AS
WITH entity_event_dedup AS (
    -- Collapse multiple article mentions of the same entity in the same event to their primary stance
    SELECT DISTINCT ON (entity_id, event_id)
        entity_id,
        event_id,
        stance,
        role_in_event
    FROM TB_Entity_Event
    ORDER BY entity_id, event_id, confidence DESC, id DESC
)
SELECT
    e1.id AS entity_a_id,
    e1.canonical_name AS entity_a_name,
    e1.entity_type AS entity_a_type,
    e2.id AS entity_b_id,
    e2.canonical_name AS entity_b_name,
    e2.entity_type AS entity_b_type,
    COUNT(DISTINCT a.event_id) AS shared_events_count,
    COUNT(DISTINCT a.event_id) FILTER (
        WHERE (a.stance IN ('SUPPORT', 'DEFENSIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))
           OR (a.stance IN ('OPPOSE', 'APOLOGETIC') AND b.stance IN ('OPPOSE', 'APOLOGETIC'))
           OR (a.stance = b.stance AND a.stance NOT IN ('NEUTRAL', 'PARTICIPANT'))
    ) AS aligned_stance_count,
    COUNT(DISTINCT a.event_id) FILTER (
        WHERE (a.stance IN ('SUPPORT', 'DEFENSIVE') AND b.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE'))
           OR (a.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))
    ) AS opposing_stance_count,
    ROUND(
        (
            COUNT(DISTINCT a.event_id) FILTER (
                WHERE (a.stance IN ('SUPPORT', 'DEFENSIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))
                   OR (a.stance IN ('OPPOSE', 'APOLOGETIC') AND b.stance IN ('OPPOSE', 'APOLOGETIC'))
                   OR (a.stance = b.stance AND a.stance NOT IN ('NEUTRAL', 'PARTICIPANT'))
            )::NUMERIC
            -
            COUNT(DISTINCT a.event_id) FILTER (
                WHERE (a.stance IN ('SUPPORT', 'DEFENSIVE') AND b.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE'))
                   OR (a.stance IN ('OPPOSE', 'APOLOGETIC', 'INVESTIGATIVE') AND b.stance IN ('SUPPORT', 'DEFENSIVE'))
            )::NUMERIC
        ) / NULLIF(COUNT(DISTINCT a.event_id), 0),
        3
    ) AS alignment_index, -- Ranges from -1.000 (adversaries) to +1.000 (coalition allies)
    ARRAY_AGG(DISTINCT tag) FILTER (WHERE tag IS NOT NULL) AS shared_topic_tags
FROM entity_event_dedup a
JOIN entity_event_dedup b
    ON a.event_id = b.event_id AND a.entity_id != b.entity_id
JOIN TB_Entity e1 ON e1.id = a.entity_id
JOIN TB_Entity e2 ON e2.id = b.entity_id
JOIN TB_Event ev ON ev.id = a.event_id
LEFT JOIN LATERAL unnest(ev.topic_tags) AS tag ON TRUE
GROUP BY e1.id, e1.canonical_name, e1.entity_type, e2.id, e2.canonical_name, e2.entity_type;

-- View 2: Chronological Public Figure Career & Stance Ledger
CREATE OR REPLACE VIEW VW_Entity_Career_Ledger AS
SELECT
    e.id AS entity_id,
    e.canonical_name,
    e.entity_type,
    ev.id AS event_id,
    COALESCE(ev.event_date, art.published_at::DATE) AS effective_date,
    ev.canonical_title AS event_title,
    ev.event_type,
    ev.topic_tags,
    ee.role_in_event,
    ee.stance,
    ee.action_summary,
    ee.direct_quotes,
    art.url AS source_url,
    art.title AS article_title
FROM TB_Entity e
JOIN TB_Entity_Event ee ON ee.entity_id = e.id
JOIN TB_Event ev ON ev.id = ee.event_id
JOIN TB_Article art ON art.id = ee.source_article_id
ORDER BY e.id, effective_date DESC;
