# PLAN: City council & commission staff reports (Yolo County cities)

Status: **proposed, not started** (2026-11-08). Overlaps `PLAN_MANUAL_DOCUMENTS.md` (shared PDF-to-text,
`official_record` genre, wiki badges); build the shared parts once.

## What each portal allows (probed 2026-11-08, one request at a time, our own bot User-Agent)
| Jurisdiction | Platform | Can we crawl? |
|---|---|---|
| Woodland | CivicClerk (`woodlandca.api.civicclerk.com`, public JSON API) | **Yes.** robots: `User-agent: * Allow: /`, `Content-Signal: search=yes, ai-train=no`. Names ClaudeBot/GPTBot in Disallow (same situation as the news sources the user approved). History back to 2011; council plus every commission (Planning, Parks & Rec, Library, Sustainability, Aging...). Each agenda item lists its attachments (staff report, resolution, plans). |
| Winters | CivicClerk (`wintersca.api.civicclerk.com`) | **Yes**, same terms. Only since Oct 2026 (just migrated); older material is elsewhere. |
| West Sacramento | Granicus (`westsacramento.granicus.com`); old Legistar stops in 2019 | **No.** robots.txt: `User-agent: * Disallow: /`. |
| Davis | Index pages on `cityofdavis.org` (returns 403 to our bot); PDFs on `documents.cityofdavis.org` (no robots.txt, PDFs load) | **No automatic discovery.** The only index is behind the 403; guessing PDF URLs would be working around the block. |
| Yolo County BOS | Destiny AgendaQuick (`public.destinyhosted.com/96561`) | **No.** robots.txt: `User-agent: * Disallow: /`. |

Blocked ones: ask the city/county clerk for permission or a feed (an allowlisted User-Agent, an export,
or an email subscription of agenda packets). Until then, important reports can go in by hand through the
manual-documents page.

### Second pass (2026-11-08): lawful channels for the blocked three
- cityofdavis.org, cityofwestsacramento.org and yolocounty.gov sit behind Akamai bot protection (403 even on
  robots.txt). Granicus robots.txt allows only Googlebot/Bing/Yahoo (that is how Brown Act § 54954.2
  "indexable by search engines" is met); Destiny disallows everyone. Not bypassed, not proxied (no Wayback).
- Davis `documents.cityofdavis.org` serves PDFs to us (no robots.txt) but has no index (directory URLs 404).
- **Brown Act § 54954.1**: anyone may make a written standing request to be mailed the agenda *and all
  documents in the agenda packet* for every meeting (email is allowed; fee capped at cost). This is
  the legal channel for future meetings: a dedicated inbox, plus a worker `imap` source that ingests the
  packets. Fully compliant and no crawling.
- **CPRA § 7922.570 (native electronic format)**: request staff reports, minutes and agenda packets
  2015-present as electronic files (or a vendor export). Portals: Davis GovQA (`cityofdavis.govqa.us`), West Sac
  NextRequest (`westsacramento.nextrequest.com`), county via Clerk of the Board. Imported with a bulk-import command.
- **SB 272 catalogs** (Davis: cityofdavis.org/city-hall/transparency-portal/catalog-of-enterprise-systems)
  name the agenda system and vendor, which is useful in the CPRA request ("export from <product>").

### Decisions (user, 2026-11-08)
- History floor 2015 for now (will extend later). Backlog via the API burst.
- User will contact the clerks for the blocked three.

## Design
1. **New crawl method `civicclerk`** in `crawler/scraper.py` (polite client, per-host serial, robots honoured):
   - `GET /v1/Events?$filter=startDateTime ge {since}&$orderby=startDateTime desc` -> meetings (filter by
     `categoryName` per `scrape_config.bodies`).
   - `GET /v1/Meetings/{agendaId}` -> agenda items + attachments.
   - One `TB_Article` per **agenda item with a staff report**: title = "Woodland City Council, 2026-11-05:
     Conditional Use Permit for ...", `published_at` = meeting date, url = stable item URL on the portal, text =
     item text + staff-report PDF text (pypdf; text PDFs only; cap ~40k chars).
   - Skip routine items: minutes approval, warrants/claims, proclamations, "receive and file" (title rules).
     Plans, maps and redlines are not downloaded.
2. **Genre `official_record`**: bypasses the LLM triage (the source *is* local civic news), genre weight 1.0,
   evidence `reported`. Stage 2 prompt notes: staff are authors, not stance-takers; the recommendation
   is the department's position; votes appear in minutes, not in the staff report.
3. **Sources**: "Woodland City Council & Commissions", "Winters City Council & Commissions" (reliability 1.0).
   The history walk works as for news (newest first, down to 2015).
4. **Load**: staff reports are long. Recent items go through the normal queue; the 2015+ backlog
   (likely thousands of items) is better done with an API burst (~$0.005-0.01 per report at
   current DeepSeek prices, so roughly $20-60 for the whole Woodland backlog; measure on 50 first).
5. **Wiki**: "official record" badge; the event page links the meeting and the PDF.

## Schema (v1.6, user applies)
- `TB_Article.genre` CHECK (if any) gains `'official_record'`; new `TB_Article.doc_meta JSONB`
  (body, meeting id, item id, attachment URLs).
- Reuses `TB_Sources.scrape_config` for `method: civicclerk`, `tenant`, `bodies`.

## Open decisions
1. Which bodies: City Council only, or council + all commissions?
2. History depth: same 2015 floor as news, or shorter (e.g. 2022) to limit cost?
3. Backlog: API burst (fast, ~$20-60) or local server only (months)?
4. Contact the Davis / West Sacramento / county clerks about access?

## Estimate
~half a day (method + PDF text + tests with a fake CivicClerk transport), plus ~2 h for shared parts if
the manual-documents plan has not been built yet.
