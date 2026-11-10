You are an expert web developer

## System Overview
This repository holds two cooperating projects that share one PostgreSQL database (`pgvector` + `pg_trgm`):

1. **`civic_tracker/` — the pipeline package** (spec: `SPEC_WEB_CRAWLER_PARSER.md`, Stage 3 in `SPEC_WEB_UI.md`).
   Async Python (psycopg pool, httpx, OpenAI-compatible clients). Crawls local news, runs Stage 1 NLP
   (fastcoref + GLiNER + spaCy), Stage 2 extraction (DeepSeek V4-flash), the resolver, and Stage 3 wiki bios
   (DeepSeek). It runs as a **long-lived worker process** (`civic-tracker worker`) that keeps the NLP models
   warm. It has its own venv (`civic_tracker/.venv`, Python 3.12) and config (`civic_tracker/.env`).
2. **The Flask "Civic Wiki" website — this directory (`/root`)** (spec: `SPEC_WEB_UI.md`, adapted to Flask).
   A password-protected wiki + admin panel served by **gunicorn**. It reads the database directly and controls
   the worker **only through the database** (command queue + status tables). It never runs the pipeline itself.

`SPEC_WEB_UI.md` describes a single FastAPI process with an in-process scheduler. We deliberately use
**Flask + gunicorn + a separate worker** instead; where the spec says FastAPI/HTMX/uvicorn/`lifespan`, apply the
mapping below. Everything else in the spec (pages, views, salience scoring, Stage 3, admin features) still applies.

| SPEC_WEB_UI.md                         | This codebase                                                        |
|----------------------------------------|----------------------------------------------------------------------|
| FastAPI routes in `web/public.py`, `web/admin.py` | Flask Blueprints in `api/` (`api/wiki.py`, `api/admin.py`, `api/auth.py`) |
| HTMX partials (`hx-get`, `/hx/...`)    | JSON endpoints in `api/` + `static/js/*.js` (`$.get`/`$.post`) rendering `static/hbs/*.js` templates |
| Jinja2 + Tailwind (CDN)                | Same: `templates/` + Tailwind Play CDN (no build step)               |
| In-process `PipelineWorkerManager`     | `civic_tracker/worker.py` daemon; web ↔ worker via `TB_Worker_Command` / `TB_Worker_Status` |
| `civic-tracker serve`                  | `gunicorn index:app` (web) + `civic-tracker worker` (pipeline)       |
| `run-cycle` / "Run Full Cycle Now"     | Removed. Crawling and processing are decoupled (see below)           |
| `web/filters.py` wiki-link filter      | `lib/wiki_links.py`, registered as a Jinja filter in `index.py`      |

### Crawl / process decoupling
- The **crawler only queues**: it inserts `TB_Article` rows with `processing_status = 'PENDING'`, on the
  scheduler interval (`TB_Scheduler_State`) or on demand (admin "Crawl now" / backfill commands).
- The **processor runs continuously**: each free slot atomically claims the **most recently published**
  unparsed article (`PENDING`/`STAGE1_DONE` → `IN_PROGRESS`), runs Stage 1 → 2 → resolver, and loops.
  When the queue is empty it synthesizes Stage 3 bios for eligible dirty entities, then idles.
- **Stage 3 eligibility**: an entity gets a bio only with ≥ 3 events, or ≥ 2 events and ≥ 1 affiliation.
- **Crawling must be gentle** (local papers): all crawler HTTP goes through `civic_tracker/crawler/polite.py`
  (one request at a time per site, ≥ 5 s apart or the site's `Crawl-delay`, honour `Retry-After`, stop a source
  after repeated 429s). Never bypass bot protection or change the User-Agent to look like a browser.

## Codebase Organization (website)
- `index.py`: Flask application entry point (`app`), registers blueprints and Jinja filters. Run with gunicorn.
- `api/`: Flask Blueprints defining routes (`auth.py`, `wiki.py`, `admin.py`).
- `lib/`: Database interface libraries (`db_conn.py`, `db_wiki.py`, `db_admin.py`, ...). **Most DB logic is here, not in API files.**
- `sql/`: Database schema and migration files (see Migrations).
- `static/js/`: Frontend logic (non-built, raw JS, jQuery).
- `static/hbs/`: Handlebars templates wrapped in JS files.
- `templates/`: Jinja2 HTML templates (server-rendered pages; Tailwind via CDN).
- `civic_tracker/`: the pipeline package (separate venv, async). The website does **not** import it (different
  venv); where the web needs pipeline logic (e.g. the Qwen3 SEARCH_QUERY instruction in `lib/embed.py`) it keeps a
  small mirrored copy - keep the two in sync.
- `gunicorn.conf.py`, `deploy/`: hosting (gunicorn + systemd units for `civic-web` and `civic-worker`).
- Website venv: `/root/.venv` (Python 3.12, `requirements.txt`).

## Critical Workflows & Established Patterns

### Database Access (website)
- **Do not use ORMs**. Use existing `lib/db_*.py` modules or extend them with raw SQL.
- **Pattern**:
  ```python
  # In lib/db_example.py
  from lib.db_conn import Db_conn
  db = Db_conn()

  def get_data(id):
      return db.fetch_all("SELECT * FROM table WHERE id = %s", (id,))
  ```
- **Connection**: `Db_conn` (sync psycopg 3 connection pool, dict rows) handles connection logic.
  `db.fetch_all`, `db.fetch_one` and `db.execute` are the primary methods.
- The pipeline package has its own async data layer (`civic_tracker/db/repositories.py`); keep the two separate.

### Database Migrations
- **Pattern**: Create `.sql` files in `sql/`
- **Application**: Allow the user to apply all database schema changes
- **Conventions**:
  - `sql/applied/`: Prod-applied schemas.
  - `sql/unapplied/`: All new schema changes and schema changes under development.
  - Keep `civic_tracker/db/schema.sql` / `schema_web.sql` in sync with migrations so a fresh DB (and the test
    suite's throwaway schema) can be built with `civic-tracker db-init` / `db-init-web`.
- Always assume the user has handled applying schema changes. All direct db changes will be made by programmer who is an expert in database and server administration.

### Frontend Development
- **Pages** are server-rendered Jinja2 templates in `templates/` extending `templates/base.html`.
- **Dynamic parts** (omnibox live search, ledger sort/topic filters, admin worker-status polling) use JSON
  endpoints + Handlebars:
- **Templates**: Handlebars templates are defined in `static/hbs/*.js` as template strings, compiled immediately into global constants.
  ```javascript
  // static/hbs/example_hbs.js
  const example_template = `<div>{{value}}</div>`;
  const example_compiled = Handlebars.compile(example_template);
  ```
- **Interaction**: `static/js/*.js` files (e.g., `wiki.js`, `admin.js`) initialize pages, make AJAX requests (`$.get`/`$.post`), and render using the global compiled templates.
- **Styling**: Tailwind CSS via the Play CDN script; no build step. Stance colours: SUPPORT=emerald,
  OPPOSE=rose, APOLOGETIC=amber, DEFENSIVE=orange, INVESTIGATIVE=purple, NEUTRAL/PARTICIPANT=slate.

### API Development
- **Blueprints**: Register new routes in `api/` modules and ensure they are registered in `index.py`.
- **Authentication**: `flask_login` with two roles and **two different passwords**:
  - **Site password** (viewer): required for every page except the login page. Stored as a hash in
    `TB_Web_Settings` and **changeable in the admin panel**. Use `@login_required`.
  - **Admin password**: unlocks `/admin/*`. Stored as a hash in the `.web` config file. Use `@admin_required`
    (`lib/auth.py`). An admin session also satisfies `@login_required`.
- Mutating admin endpoints are POST-only and CSRF-protected (token in session, sent by `static/js/common.js`).

## Project-Specific Rules
1. **Raw SQL**: Always use parameterized queries (`%s`). Never string concatenation for SQL.
2. **Global DB Instance**: `lib` modules typically instantiate a global `db = Db_conn()` at the top level.
3. **No Build Step**: JS and CSS are served raw. Do not introduce webpack/npm workflows unless explicitly requested.
4. **Environment**: DB config is read from a `.db` file or defaults to hardcoded dev values in `Db_conn`. Other config files may be added as needed following this pattern. ex. `.web` for the website (secret key, admin password hash, alert webhook), `.file_service` for settings related to the file service. The pipeline reads `civic_tracker/.env`. Any hardcoded config should not be submitted to github or may differ between dev and prod.
5. **Web never runs the pipeline**: admin actions enqueue `TB_Worker_Command` rows; the worker executes them and reports progress in `TB_Worker_Status` / `TB_Job_Run`.
6. **Data is internal**: the wiki republishes newspaper text and quotes, so it stays behind the site password on the LAN.
