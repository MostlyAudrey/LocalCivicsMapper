# PLAN: Manual one-time documents (campaign websites, official records, press releases)

Status: **planned, not started** (agreed 2026-11-08; build when Claude Code credit allows).
Related: `SPEC_WEB_CRAWLER_PARSER.md`, `SPEC_WEB_UI.md`, `CLAUDE.md`, migrations `sql/unapplied/v1.5_SourceWeighting.sql`.

## Goal
Let an admin feed the wiki material that is not a regularly crawled news article: a candidate's campaign
website, a candidate questionnaire, a council agenda / minutes / staff report (PDF), an agency press release,
a voter guide. The main motivation is balance: today an entity's positions can come almost entirely from
third-party commentary (e.g. Jane Roe: 37 of 38 stance rows came from Davisite opinion pieces). Adding the
entity's *own* stated positions gives the weighting system (v1.5) a direct, attributable counterweight.

## 1. Admin -> "Add document" page (Flask, `api/admin.py`, `templates/admin/documents.html`)
- Input, one of:
  - one or more URLs;
  - an uploaded PDF;
  - pasted text (for pages behind JavaScript or bot protection - never bypass protection).
- **Document type**: `campaign` (campaign site / candidate statement or questionnaire), `official_record`
  (agenda, minutes, staff report, resolution), `press_release`, `other`.
- **Subject entity** (optional): whose material it is, chosen with the existing entity picker (merge page
  pattern, `/admin/api/entities`).
- **Date**: when the material is from (defaults to today).
- **Campaign sites**: optional "also fetch linked pages on the same site" (Issues, About, Endorsements),
  capped at ~10 pages, one at a time through `crawler/polite.py` (robots.txt, delays, 429 back-off).
- List of added documents with status, **Refresh** and **Remove** buttons.

## 2. Pipeline (worker; the web app never fetches - CLAUDE.md rule 5)
- Admin form enqueues a `TB_Worker_Command` (`INGEST_DOCUMENT`, `REFRESH_DOCUMENT`); the worker fetches via
  the polite client; PDFs -> text locally (pypdf). Uploaded files / pasted text are stored with the command.
- Each page/document becomes a `TB_Article` row under a built-in **"Manual documents"** source, with
  `doc_kind` and `subject_entity_id`; triage is bypassed (admin chose it: `skip_reason = 'override'`) and it is
  processed promptly (newest-first claim with today's date).
- Stage 2 prompt gets the document type and subject:
  - On X's campaign material, X's stated positions are `evidence = self`; anything X says about opponents is
    `evidence = claimed`, `claimed_by = X`.
  - Official records are `reported`.
- Platform positions are not events: new event type **`CAMPAIGN_PLATFORM`**, one event per issue, e.g.
  "Jane Roe 2026 platform: foreign policy". Lets the wiki answer "where does X *say* they stand?".

## 3. Weighting (reuses v1.5 `VW_Entity_Event_Weighted`)
- "Manual documents" source reliability 1.0.
- Official records: weight 1.0 (reported). Campaign material: 1.0 x self 0.8 = 0.8 -> outweighs op-ed claims
  (~0.03-0.1) while staying labelled as the candidate's own claims.

## 4. Lifecycle
- **Refresh**: re-fetch; if `content_hash` changed, re-extract with `Orchestrator.reextract(...,
  replace_existing=True)` (already built: replaces rows, keeps disputed rows, deletes orphans).
- **Remove**: `clear_article_extraction` + `delete_orphans`, then delete the article row.

## 5. Wiki
- Ledger badges: "campaign site", "official record" (next to the existing "opinion" / "claimed by" badges).
- Entity page: **Stated positions** section built from the entity's own `CAMPAIGN_PLATFORM` / `self` rows.
- Bio writer (`stage3_wiki.py`): may cite stated positions as "X's campaign says ...".

## 6. Schema (migration v1.6, user applies; mirror as `civic_tracker/db/schema_web_v16.sql`)
- `TB_Article`: `doc_kind TEXT`, `subject_entity_id INTEGER REFERENCES TB_Entity(id) ON DELETE SET NULL`.
- `TB_Event.event_type` CHECK: add `'CAMPAIGN_PLATFORM'` (and the Pydantic `EventType` literal).
- `TB_Worker_Command.command` CHECK: add `'INGEST_DOCUMENT'`, `'REFRESH_DOCUMENT'`.
- Insert the built-in "Manual documents" source (reliability 1.0, `is_active = FALSE` so scheduled crawls skip it).

## Open decisions (ask before building)
1. Campaign-site depth: follow same-site links up to 10 pages, or only the URLs given?
2. PDFs: text PDFs only for now (skip scanned / OCR)?
3. Dedicated `CAMPAIGN_PLATFORM` event type, or fold positions into existing event types?

## Estimate
~half a day of work: migration, worker fetch + PDF handling, admin page, Stage 2 prompt rules, wiki badges,
tests (fake fetch transport + fake extractor, as in `tests/test_pipeline_e2e.py`).
