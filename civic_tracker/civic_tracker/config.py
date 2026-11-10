"""Runtime settings.

Values are read from environment variables or a `.env` file in the working directory.
Defaults are deliberately generic (localhost); real hosts belong in `.env`, which is
git-ignored. See `.env.example`.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# civic_tracker/.env (project root) is always read; a .env in the working directory overrides it.
_PROJECT_ENV = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(_PROJECT_ENV, ".env"), env_file_encoding="utf-8", extra="ignore")

    # Password may be omitted: libpq falls back to ~/.pgpass.
    DATABASE_URL: str = "postgresql://postgres@localhost:5432/Davis"
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 10

    # DeepSeek V4-flash endpoint (OpenAI-compatible vLLM / SGLang)
    LLM_BASE_URL: str = "http://localhost:8000/v1"
    LLM_API_KEY: str = "local"
    LLM_MODEL: str = "deepseek-ai/DeepSeek-V4-Flash"
    LLM_MAX_CONCURRENCY: int = 6
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_TOKENS: int = 32000      # reasoning + JSON; server context is 128K
    LLM_TIMEOUT_SECONDS: float | None = None  # no timeout: slow generations finish; durations go to TB_LLM_Call
    # DeepSeek V4 reasoning mode (chat_template_kwargs.thinking). On: ~12 min/article on 2x DGX Spark
    # but clean fields. Off: ~2-3 min/article, but the model tends to "think" inside JSON strings.
    LLM_THINKING: bool = True

    # Local Qwen/Qwen3-Embedding-4B endpoint
    EMBED_BASE_URL: str = "http://localhost:8002/v1"
    EMBED_API_KEY: str = "local"
    EMBED_MODEL: str = "Qwen/Qwen3-Embedding-4B"
    EMBED_DIM: int = 1024  # MRL truncated dimension (pgvector HNSW limit is 2000 for float32)
    EMBED_BATCH_SIZE: int = 64

    # Stage 1 local models
    SPACY_MODEL: str = "en_core_web_sm"
    COREF_DEVICE: str = "cpu"  # e.g. "cuda:0"
    GLINER_MODEL: str = "knowledgator/gliner-relex-large-v1.0"
    GLINER_DEVICE: str = "cpu"
    GLINER_THRESHOLD: float = 0.5
    # Smaller sentence-aligned chunks improve recall (gliner-relex misses mentions in long contexts).
    GLINER_CHUNK_CHARS: int = 600

    # Triage: skip non-local / non-news articles before any NLP or LLM work (pipeline/triage.py)
    TRIAGE_ENABLED: bool = True

    # API burst mode (admin "Burst" card): extra OpenAI-compatible endpoint for clearing the backlog fast.
    # Defaults target DeepSeek's own API; check the current model id + prices on api-docs.deepseek.com.
    BURST_PROVIDER_NAME: str = "deepseek-api"
    BURST_LLM_BASE_URL: str = "https://api.deepseek.com"
    BURST_LLM_API_KEY: str | None = None
    BURST_LLM_MODEL: str = "deepseek-flash"
    BURST_RESPONSE_FORMAT: str = "json_object"   # DeepSeek API: JSON mode (schema in prompt + validation)
    BURST_EXTRA_BODY: str = "{}"                 # JSON, e.g. '{"thinking": {"type": "disabled"}}'
    BURST_THINKING: bool = True                  # label only (what the endpoint does by default)
    BURST_MAX_TOKENS: int = 32000
    BURST_CONCURRENCY: int = 8
    BURST_PRICE_INPUT_PER_M: float = 0.30        # USD per 1M input tokens (peak; cache hits are cheaper)
    BURST_PRICE_OUTPUT_PER_M: float = 1.20       # USD per 1M output tokens (peak)
    BURST_DEFAULT_SPEND_CAP_USD: float = 5.0
    # When the local model's output hits LLM_MAX_TOKENS (almost always a repetition loop that ties up a slot
    # for minutes), send that article to the burst API model instead of retrying locally. Needs
    # BURST_LLM_API_KEY; capped per UTC day (then it falls back to one local retry).
    TRUNCATION_FALLBACK_TO_API: bool = True
    TRUNCATION_FALLBACK_DAILY_USD: float = 2.0

    # Worker daemon (civic-tracker worker)
    WORKER_SLOTS: int = 4                 # parallel article slots (concurrent LLM requests)
    WORKER_IDLE_SLEEP_SECONDS: float = 60.0
    BIO_BATCH_LIMIT: int = 25             # Stage 3 bios per idle period before re-checking the queue
    BIO_INTERLEAVE_MINUTES: float = 30.0  # while articles are queued, still write one bio this often (0 = idle only)
    ALERT_WEBHOOK_URL: str | None = None  # Mattermost / Slack / Discord / Home Assistant JSON webhook

    # History walk: every crawl, right after a source's recent check, queue this many older articles from it,
    # back to the floor. Feed/links sources walk back through the site's sitemap
    # (scrape_config.history_method overrides; "none" disables it for a source).
    HISTORY_ENABLED: bool = True
    HISTORY_BATCH_PER_SOURCE: int = 20
    # Queue cap for the history walk: while fewer than this many articles wait for processing, history-only
    # rounds run (all sources, one batch each, at most once per HISTORY_ROUND_GAP_MINUTES); once the queue
    # reaches it, the walk stops, even mid-round. Recent (new) articles are always queued.
    HISTORY_QUEUE_MAX: int = 200
    HISTORY_ROUND_GAP_MINUTES: float = 15.0
    CRAWL_HISTORY_FLOOR: str = "2015-01-01"   # per-source override: scrape_config.history_floor

    # Crawler
    # Minimum seconds between requests to one site. Raised per source by scrape_config.crawl_delay
    # or the site's robots.txt Crawl-delay, whichever is larger. Be gentle: these are local papers.
    CRAWL_DELAY_SECONDS: float = 5.0
    CRAWL_MAX_THROTTLES: int = 3   # consecutive 429/503s before giving up on a source for this run
    # Random spacing on top of the minimum delay: each gap is delay x (1 + uniform(0, JITTER_FACTOR)),
    # i.e. 5-15 s at the defaults, and with probability PAUSE_CHANCE a long break of PAUSE_MIN-MAX
    # seconds is added (about one 2-10 minute break every ~25 requests).
    CRAWL_JITTER_FACTOR: float = 2.0
    CRAWL_PAUSE_CHANCE: float = 0.04
    CRAWL_PAUSE_MIN_SECONDS: float = 120.0
    CRAWL_PAUSE_MAX_SECONDS: float = 600.0
    # Scheduled crawls start up to this fraction of the interval late, at random (0.25 x 6 h = up to 90 min).
    CRAWL_SCHEDULE_JITTER: float = 0.25
    CRAWL_TIMEOUT_SECONDS: float = 30.0
    # Sources crawled at the same time. Politeness stays per site: sources that share a host
    # (base_url / rss_url / wp_api_base / listing_urls) are always crawled one after another.
    CRAWL_PARALLEL_SOURCES: int = 6
    USER_AGENT: str = "CivicTrackerBot/1.0 (+local-research)"


settings = Settings()
