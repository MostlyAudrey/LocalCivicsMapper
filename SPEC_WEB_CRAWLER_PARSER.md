# SPEC.md: Civic Entity, Event, and Coalition Intelligence Pipeline

## 1. System Overview & Objective
Build a modular, production-grade Python 3.11+ application backed by PostgreSQL (`pgvector` + `pg_trgm`) that crawls local news websites, supports retroactive historical backfilling, and runs a two-stage hybrid NLP + Local LLM pipeline:
1. **Stage 1 (Local NLP + Semantic Gatekeeper)**: `fastcoref` for pronoun/coreference resolution + `GLiNER` (`GLiNER-Relex` / `GLiNER2.5`) for zero-shot entity/relation pre-scanning + `Qwen/Qwen3-Embedding-4B` for paragraph clustering and semantic deduplication.
2. **Stage 2 (Local Structured LLM)**: `DeepSeek V4-flash` (served via an OpenAI-compatible vLLM/SGLang endpoint) using strict JSON Schema guided decoding to extract canonical events, organizational affiliations, stances, actions, and verbatim quotes.

The database tracks public figures' career trajectories (`TB_Entity_Affiliation`), links multiple articles to deduplicated real-world events (`TB_Article_Event`), records each actor's specific actions and quotes (`TB_Entity_Event`), and computes coalition alignment / co-occurrence matrices across policy topics.

---

## 2. Tech Stack & Dependencies (`pyproject.toml`)
Do **not** install `sentence-transformers` in the worker environment; all vector embeddings are offloaded to the local `Qwen/Qwen3-Embedding-4B` OpenAI-compatible server.

- **Python**: `>=3.11`
- **Database**: PostgreSQL 16+ with `vector` (`pgvector`) and `pg_trgm` extensions.
- **DB Driver**: `psycopg[binary,pool]>=3.2` (async/sync pool) + `pgvector`.
- **Crawler & HTML Extraction**: `trafilatura>=2.3.0` (sitemaps, RSS feeds, boilerplate stripping, `htmldate` extraction), `httpx`, `tenacity`.
- **Stage 1 NLP**:
  - `spacy>=3.7` (`en_core_web_sm`) for sentence boundary detection.
  - `fastcoref>=2.1.6` for coreference resolution.
  - `gliner` (`knowledgator/gliner-relex-large-v1.0`) for fast zero-shot entity & relation span detection.
- **Stage 2 LLM & Embedding SDK**: `openai>=1.50`, `pydantic>=2.9`, `pydantic-settings>=2.5`.
- **CLI & Scheduler**: `typer>=0.12`, `rich`, `apscheduler>=3.10`.
- **Testing**: `pytest`, `pytest-asyncio`.

---

## 3. Repository Structure
```text
civic_tracker/
├── pyproject.toml
├── .env.example
├── civic_tracker/
│   ├── __init__.py
│   ├── config.py              # Pydantic BaseSettings
│   ├── db/
│   │   ├── connection.py      # psycopg AsyncConnectionPool & schema init
│   │   ├── schema.sql         # Complete DDL + Coalition Views
│   │   ├── repositories.py    # CRUD, Entity Resolution, Event Vector Dedup, Dossier Queries
│   ├── crawler/
│   │   ├── discovery.py       # RSS & Sitemap discovery + retroactive date-window filtering
│   │   ├── scraper.py         # Trafilatura downloading, htmldate verification, SHA256 hashing
│   ├── pipeline/
│   │   ├── encoder.py         # Qwen/Qwen3-Embedding-4B client (Instruct templates + 1024-dim MRL)
│   │   ├── stage1_nlp.py      # fastcoref + GLiNER candidate span & sentence bundler
│   │   ├── stage2_llm.py      # DeepSeek V4-flash structured output client (Pydantic schemas)
│   │   ├── resolver.py        # Hybrid pg_trgm + Qwen3 vector entity/event linker
│   │   ├── orchestrator.py    # End-to-end batch processor with error recovery
│   └── cli.py                 # Typer CLI (source management, crawl, process, dossier, coalition)
└── tests/
    └── test_pipeline_e2e.py   # End-to-end test using the Davis Vanguard ICE stop fixture
```

---

## 4. Configuration (`civic_tracker/config.py`)
```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql://postgres:postgres@192.168.86.147:5400/Davis"

    # DeepSeek V4-flash Endpoint (OpenAI-compatible vLLM / SGLang)
    LLM_BASE_URL: str = "http://100.91.197.57:8000/v1"
    LLM_API_KEY: str = "local"
    LLM_MODEL: str = "deepseek-ai/DeepSeek-V4-Flash"
    LLM_MAX_CONCURRENCY: int = 6

    # Local Qwen/Qwen3-Embedding-4B Endpoint
    EMBED_BASE_URL: str = "http://100.91.197.57:8000/v1
    EMBED_API_KEY: str = "local"
    EMBED_MODEL: str = "Qwen/Qwen3-Embedding-4B"
    EMBED_DIM: int = 1024          # MRL truncated dimension (pgvector HNSW limit is 2000 for float32)
    EMBED_BATCH_SIZE: int = 64

    # Crawler Settings
    CRAWL_DELAY_SECONDS: float = 1.5
    USER_AGENT: str = "CivicTrackerBot/1.0 (+local-research)"

settings = Settings()
```

---

## 5. Complete Database Schema & Analytical Views (`civic_tracker/db/schema.sql`)

```sql
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
```

---

## 6. Local `Qwen/Qwen3-Embedding-4B` Client (`civic_tracker/pipeline/encoder.py`)

`Qwen/Qwen3-Embedding-4B` has a native 2560-dimension output and uses instruction prefixes (`Instruct: ...\nQuery: ...`) for queries and symmetric clustering/deduplication tasks, while asymmetric target documents are encoded without a prefix. We apply Matryoshka Representation Learning (MRL) slicing to `1024` dimensions followed by L2 renormalization so standard `pgvector` HNSW indexes operate at full speed.

```python
import math
from typing import List, Literal
from openai import AsyncOpenAI
from civic_tracker.config import settings

TaskType = Literal[
    "ENTITY_DEDUP",      # Symmetric: matching TB_Entity profiles across articles
    "EVENT_DEDUP",       # Symmetric: matching TB_Event records across articles
    "ARTICLE_CLUSTER",   # Symmetric: clustering paragraphs or detecting duplicate wire stories
    "SEARCH_QUERY",      # Asymmetric Query: CLI semantic search over positions/quotes
    "SEARCH_DOCUMENT",   # Asymmetric Document: storing TB_Entity_Event.action_summary
]

TASK_INSTRUCTIONS = {
    "ENTITY_DEDUP": (
        "Given a civic entity profile with its name, type, role, and affiliated organization, "
        "retrieve or match equivalent profiles referring to the same real-world person or organization."
    ),
    "EVENT_DEDUP": (
        "Given a summary of a local news event or civic decision, match it with descriptions "
        "of the exact same underlying real-world incident, meeting, or controversy."
    ),
    "ARTICLE_CLUSTER": (
        "Identify the core civic topic, policy issue, and actors discussed in this news passage."
    ),
    "SEARCH_QUERY": (
        "Given a policy issue, stance, or civic topic query, retrieve relevant public official "
        "actions, decisions, and verbatim statements."
    ),
}

def format_qwen3_input(text: str, task: TaskType) -> str:
    cleaned = " ".join(text.strip().split())
    if task == "SEARCH_DOCUMENT":
        # Asymmetric documents are encoded raw without instruction prefix per Qwen3 spec
        return cleaned
    instruct = TASK_INSTRUCTIONS[task]
    return f"Instruct: {instruct}\nQuery: {cleaned}"

def truncate_and_l2_normalize(vec: List[float], dim: int) -> List[float]:
    sliced = vec[:dim]
    norm = math.sqrt(sum(x * x for x in sliced))
    if norm == 0.0:
        return sliced
    return [x / norm for x in sliced]

class LocalEncoderClient:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url=settings.EMBED_BASE_URL,
            api_key=settings.EMBED_API_KEY,
        )
        self.model = settings.EMBED_MODEL
        self.dim = settings.EMBED_DIM
        self.batch_size = settings.EMBED_BATCH_SIZE

    async def embed(self, texts: List[str], task: TaskType) -> List[List[float]]:
        if not texts:
            return []
        formatted = [format_qwen3_input(t, task) for t in texts]
        results: List[List[float]] = []

        for i in range(0, len(formatted), self.batch_size):
            batch = formatted[i : i + self.batch_size]
            resp = await self.client.embeddings.create(
                model=self.model,
                input=batch,
                encoding_format="float",
            )
            sorted_data = sorted(resp.data, key=lambda item: item.index)
            for item in sorted_data:
                results.append(truncate_and_l2_normalize(item.embedding, self.dim))
        return results
```

---

## 7. Web Crawler & Retroactive Backfill (`civic_tracker/crawler/`)

### `discovery.py` & `scraper.py` Requirements
1. **Incremental Crawl (`crawl --recent`)**:
   - Poll `TB_Sources.rss_url` via `trafilatura.feeds.find_feed_urls` and latest sitemaps.
   - Filter out URLs already present in `TB_Article.url`.
2. **Retroactive Seeding (`crawl --backfill-days N` or `--since YYYY-MM-DD`)**:
   - Fetch the source's `sitemap_index.xml` (supporting standard WordPress `/wp-sitemap.xml` and Yoast `/sitemap_index.xml`).
   - Parse `<sitemap>` and `<url>` entries, pre-filtering by `<lastmod>` or URL path date patterns (`/YYYY/MM/`) before making HTTP requests to avoid downloading years of unrelated pages.
   - Download HTML with `httpx` (respecting `settings.CRAWL_DELAY_SECONDS`) and extract content via:
     ```python
     extracted = trafilatura.bare_extraction(
         html_content,
         url=url,
         include_links=True,
         include_Quotes=True,
         with_metadata=True,
     )
     ```
   - Parse `extracted["date"]` into a timezone-aware `published_at` timestamp. Confirm it falls inside the requested backfill window, compute `hashlib.sha256(full_text.encode()).hexdigest()`, and insert into `TB_Article` with `processing_status = 'PENDING'`.

---

## 8. Two-Stage Extraction Pipeline (`civic_tracker/pipeline/`)

### Stage 1: Coreference & Zero-Shot Entity Pre-Filter (`stage1_nlp.py`)
1. **Coreference Resolution (`fastcoref`)**:
   - Run `FCoref` over `TB_Article.full_text`.
   - For pronoun mentions (`he`, `she`, `they`, `his`, `her`) that belong to a coreference cluster whose head noun phrase is a proper noun, annotate inline in brackets: e.g., `"She [Gloria Ravel] apologized for the incident."` Save as `TB_Article.resolved_text`.
2. **Zero-Shot Entity & Span Detection (`GLiNER`)**:
   - Run `GLiNER` over `resolved_text` with labels:
     `["person", "public official", "law enforcement officer", "government agency", "activist organization", "city council", "coalition"]`.
   - If zero entities are found, mark the article `SKIPPED` to save LLM compute.
   - Segment `resolved_text` into sentences with `spaCy`. Retain every sentence containing at least one detected entity plus `±1` surrounding context sentence. Save this condensed text as `TB_Article.evidence_bundle` along with a deduplicated list of `candidate_entities`.

### Stage 2: Structured Extraction with `DeepSeek V4-flash` (`stage2_llm.py`)
Use `AsyncOpenAI` pointed at `settings.LLM_BASE_URL` (`deepseek-v4-flash`) with `beta.chat.completions.parse` or `response_format={"type": "json_schema", ...}` enforcing this exact Pydantic schema:

```python
from pydantic import BaseModel, Field
from typing import List, Optional, Literal

EntityType = Literal["PERSON", "ORGANIZATION", "GOVERNMENT_BODY", "COALITION"]
StanceType = Literal["SUPPORT", "OPPOSE", "NEUTRAL", "APOLOGETIC", "DEFENSIVE", "INVESTIGATIVE", "PARTICIPANT"]
EventType = Literal[
    "LEGISLATIVE_VOTE", "POLICY_ANNOUNCEMENT", "PUBLIC_CONTROVERSY",
    "LAW_ENFORCEMENT_ACTION", "MEETING", "ELECTION", "LEGAL_ACTION", "OTHER"
]

class ExtractedAffiliation(BaseModel):
    person_name: str = Field(description="Full canonical name of the person without title prefix")
    organization_name: Optional[str] = Field(default=None, description="Canonical name of the organization or body")
    role_title: str = Field(description="Official role or title, e.g., 'Mayor', 'City Manager', 'Police Officer'")

class ExtractedInvolvement(BaseModel):
    entity_name: str = Field(description="Canonical name of the person, agency, or organization")
    entity_type: EntityType
    role_in_event: str = Field(description="Specific role, connection, or purpose in this event")
    stance: StanceType
    action_summary: str = Field(description="1-2 sentences detailing their specific actions, decisions, or statements")
    direct_quotes: List[str] = Field(default_factory=list, description="Exact verbatim quotes attributed to this entity")

class ExtractedEvent(BaseModel):
    canonical_title: str = Field(description="Standardized, objective title for the real-world event")
    event_type: EventType
    event_date_iso: Optional[str] = Field(default=None, description="YYYY-MM-DD resolved against article published_at")
    summary: str = Field(description="2-3 sentence objective summary of the event")
    topic_tags: List[str] = Field(description="2-5 lowercase policy tags, e.g., ['immigration', 'policing', 'civil-rights']")
    involvements: List[ExtractedInvolvement]

class ArticleExtractionResult(BaseModel):
    affiliations: List[ExtractedAffiliation]
    events: List[ExtractedEvent]
```

**System Prompt Rules for `DeepSeek V4-flash`**:
- Pass `Article Title`, `Publication Date (ISO)`, `GLiNER Candidate Entities`, and the coreference-resolved `Evidence Bundle`.
- Strip titles/honorifics (`Mayor`, `Officer`, `Councilmember`, `Dr.`) from `person_name` and `entity_name` and place them into `ExtractedAffiliation.role_title` instead. (Exception: if only a title + surname is known in the article, such as `"Officer Pemberton"`, retain `"Officer Pemberton"` as the name until a first name is discovered).
- Resolve relative dates (`"last Wednesday"`, `"yesterday"`) using the article's `published_at` date.

---

## 9. Hybrid Entity & Event Resolution (`civic_tracker/pipeline/resolver.py`)

### 1. Entity Disambiguation (`resolve_or_create_entity`)
For every extracted entity:
1. Build its profile string: `f"Name: {name} | Type: {entity_type} | Role: {role_title or 'N/A'} | Org: {org_name or 'Local'}"` and embed it via `LocalEncoderClient.embed([profile], task="ENTITY_DEDUP")`.
2. Run the hybrid lookup query against `TB_Entity`:
   ```sql
   SELECT id, canonical_name, aliases,
          similarity(canonical_name, %(name)s) AS trgm_score,
          1 - (context_embedding <=> %(embed)s::vector) AS vec_score
   FROM TB_Entity
   WHERE entity_type = %(etype)s
     AND (
       LOWER(canonical_name) = LOWER(%(name)s)
       OR LOWER(%(name)s) = ANY(SELECT LOWER(unnest(aliases)))
       OR (similarity(canonical_name, %(name)s) > 0.70 AND (1 - (context_embedding <=> %(embed)s::vector)) >= 0.85)
       OR (1 - (context_embedding <=> %(embed)s::vector)) >= 0.91
     )
   ORDER BY
     (CASE WHEN LOWER(canonical_name) = LOWER(%(name)s) THEN 1.0 ELSE 0.0 END) DESC,
     (similarity(canonical_name, %(name)s) * 0.4 + (1 - (context_embedding <=> %(embed)s::vector)) * 0.6) DESC
   LIMIT 1;
   ```
3. If matched and `%(name)s` is not already `canonical_name` or in `aliases`, append `%(name)s` to `TB_Entity.aliases`. If unmatched, insert a new row into `TB_Entity`.
4. Upsert `TB_Entity_Affiliation` updating `first_seen_date = LEAST(first_seen_date, published_at)` and `last_seen_date = GREATEST(last_seen_date, published_at)`.

### 2. Two-Tier Cross-Article Event Deduplication (`resolve_or_create_event`)
When multiple articles cover the same incident over several weeks:
1. Embed `f"{event.canonical_title}: {event.summary}"` via `LocalEncoderClient.embed(..., task="EVENT_DEDUP")`.
2. Query candidate events within `±21 days` of `COALESCE(event_date, published_at::DATE)`:
   ```sql
   SELECT ev.id,
          1 - (ev.embedding <=> %(embed)s::vector) AS cosine_sim,
          ARRAY_AGG( DISTINCT ee.entity_id ) AS existing_entity_ids
   FROM TB_Event ev
   LEFT JOIN TB_Entity_Event ee ON ee.event_id = ev.id
   WHERE ev.event_date BETWEEN (%(target_date)s::DATE - INTERVAL '21 days')
                           AND (%(target_date)s::DATE + INTERVAL '21 days')
   GROUP BY ev.id
   HAVING (1 - (ev.embedding <=> %(embed)s::vector)) >= 0.74
   ORDER BY cosine_sim DESC;
   ```
3. **Merge Decision**:
   - **Tier 1**: If top candidate has `cosine_sim >= 0.82`, reuse `ev.id`.
   - **Tier 2**: If top candidate has `0.74 <= cosine_sim < 0.82` AND shares **at least 2 resolved `entity_id`s** with the current event's participants, reuse `ev.id`.
   - Otherwise, insert a new row into `TB_Event`.
4. Link `(article_id, event_id)` in `TB_Article_Event`.
5. Batch-embed each involvement's `f"{inv.role_in_event} ({inv.stance}): {inv.action_summary} Quotes: {' '.join(inv.direct_quotes)}"` with `task="SEARCH_DOCUMENT"` and upsert into `TB_Entity_Event`.

---

## 10. CLI Interface (`civic_tracker/cli.py`)

Implement the following `typer` commands with `rich` table output:
1. `civic-tracker db-init`: Executes `schema.sql` against `DATABASE_URL`.
2. `civic-tracker source-add --name "Davis Vanguard" --url "https://davisvanguard.org" --sitemap "https://davisvanguard.org/sitemap_index.xml"`
3. `civic-tracker crawl [--source-id INT] [--backfill-days INT] [--since YYYY-MM-DD]`
4. `civic-tracker process [--batch-size 25] [--concurrency 6]`: Runs Stage 1 -> Stage 2 -> Resolver on all `PENDING` articles.
5. `civic-tracker dossier "<entity_name>" [--topic TEXT]`:
   - Queries `TB_Entity`, `TB_Entity_Affiliation`, `VW_Entity_Career_Ledger`, and `VW_Entity_Coalition_Matrix`.
   - Prints:
     1. **Known Aliases & Career Roles Timeline** (`role_title`, `organization`, `first_seen` -> `last_seen`).
     2. **Chronological Event & Position Ledger** (optionally filtered by `--topic`), including stance, action summary, verbatim quotes, and article link.
     3. **Top Coalition Allies** (`alignment_index > 0`, ordered by `shared_events_count DESC, alignment_index DESC`) and **Frequent Counterparts / Opponents** (`alignment_index < 0`).
6. `civic-tracker search-positions "<semantic_query>" [--entity "<entity_name>"]`:
   - Embeds `<semantic_query>` with `task="SEARCH_QUERY"` and ranks `TB_Entity_Event` records by cosine similarity `1 - (action_embedding <=> query_vec)`.

---

## 11. End-to-End Verification (`tests/test_pipeline_e2e.py`)

Create a self-contained test fixture representing the article `https://news.example/2026/11/davis-mayor-apologizes-observer-stop/` containing references, actions, and statements for:
`Helen Marsh`, `Gloria Ravel`, `Robin Hale`, `Valley Watch Network`, `ICE`, `Officer Pemberton`, `Davis Police Department`, `Owen Brackett`, and `Lena Okafor`.

The test must:
1. Run Stage 1 (`fastcoref` + `GLiNER`) and assert all 9 target entities appear in the extracted `evidence_bundle`.
2. Run Stage 2 (`DeepSeek V4-flash` or mock structured response in unit test mode + live integration flag `--run-live`) and `resolver.py`.
3. Assert `TB_Entity` contains all 9 actors with proper `entity_type` classifications (`PERSON`, `GOVERNMENT_BODY`, `ORGANIZATION`).
4. Simulate a second follow-up article 5 days later covering the same incident and verify that `resolve_or_create_event` links the second article to the **same** `TB_Event.id` via `TB_Article_Event` rather than creating a duplicate event.
5. Execute the `dossier` and `VW_Entity_Coalition_Matrix` queries and assert non-empty coalition alignment results.
