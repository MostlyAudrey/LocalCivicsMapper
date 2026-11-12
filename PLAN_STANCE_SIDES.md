# Plan: record *sides*, not just stances (root fix for coalition alignment)

Status: approved 2026-11-12 (decisions at the end); in progress.

## The problem

A stance (SUPPORT, OPPOSE, …) is recorded toward "the event's central action". Two entities count as allies
when their stances on the same event have the same polarity. That assumes every event has one action and two
sides (for or against). Many events don't:

| Event (real rows) | What gets recorded | What the coalition score concludes | Truth |
|---|---|---|---|
| A poll story: candidate A leads candidate B | A SUPPORT, B SUPPORT, B's campaign manager SUPPORT | A and B's staff are **allies** | opponents |
| A Super PAC spends to oppose candidate B | PAC OPPOSE, A SUPPORT | PAC and A are **opponents** | same side |
| The primary election | A PARTICIPANT, A's campaign manager DEFENSIVE | no alignment (diluted the score until v1.9) | same side |

So candidate B's campaign manager shows as a +57% ally of candidate A. The polarity of a stance
only means something relative to *what* it is aimed at, and the target is never recorded.

How much is affected: 1,112 of 2,955 events have two or more entities taking a stance (669 have three or more).
Elections (107 events), lawsuits, and controversies (critics vs. defenders) are the worst cases. A single
up-or-down decision (one ordinance, for or against) is handled fine today.

## The fix: each multi-sided event lists its sides, and each involvement says which side it is on

An event can name 2–4 **sides**, e.g. *"Candidate A campaign"* / *"Candidate B campaign"*, *"Plaintiffs
(Davis residents)"* / *"City of Davis"*, *"For Measure J"* / *"Against Measure J"*, *"Critics of the stop"* /
*"Woodland PD"*. Each involvement gets an optional `side`. Stance stays as it is: it still says *how* the
entity acted (defended, apologised, investigated…).

Pairwise alignment on a shared event then becomes:
1. **Both entities have a side:** same side → aligned; different sides → opposed. Stance polarity is ignored.
2. **Otherwise:** today's stance rules (v1.9), which work for single-question events.

For the rows above:
- Poll story: A and B are on different sides → opposed. B's campaign manager sits on B's side → opposed to A.
- Super PAC: it opposes B, so it sits on A's side → aligned with A.
- Primary: A and A's campaign manager are on the same side → aligned.

### Where sides come from (cheapest first)
1. **Roll calls (no LLM).** For a minutes vote with dissent, the sides are "aye" and "no", read directly from
   `TB_Vote`. This is exact.
2. **Structure (no LLM).** Campaign staff go on their candidate's side (the campaign link added in `ca3c4ef`).
   A spokesperson goes on their organization's side. Used only to fill gaps, never to override the article.
3. **New extractions.** Stage 2 asks for `sides` per event plus `side` per involvement, with these rules:
   - every candidate in a race is a side;
   - endorsers, donors, staff and PACs go on the side they help (opposing X means helping X's rival when there
     are two candidates; leave it null when there are several);
   - reporters, moderators and officials running the election get no side;
   - leave sides empty when nothing is contested.
4. **Backfill of existing events (LLM, small).** A new worker job, `assign-sides`. It does not re-extract the
   article. For each event with two or more stance-taking entities, it sends one short prompt (title, summary,
   the involvement list with roles and action summaries) and gets back sides plus one side per involvement.
   It touches only `side_id`; stances, quotes and summaries stay as they are.

## Changes

**Schema (v2.0 migration in `sql/unapplied/`, mirrored as `schema_web_v20.sql`)**
- `TB_Event_Side (id, event_id → TB_Event ON DELETE CASCADE, label TEXT, side_kind TEXT CHECK IN
  ('candidate','position','party','other'), source TEXT CHECK IN ('extraction','roll_call','backfill','manual'),
  UNIQUE(event_id, lower(label)))`
- `TB_Entity_Event_Side (entity_event_id → TB_Entity_Event ON DELETE CASCADE, side_id → TB_Event_Side ON DELETE CASCADE, PRIMARY KEY (both))`: an involvement can be on more than one side (an endorser of two candidates)
- `VW_Entity_Coalition_Matrix`: per pair-event, `aligned` / `opposed` come from the sides when both rows have
  one, and from the v1.9 stance rules otherwise. New columns at the end (so `CREATE OR REPLACE` still works):
  `same_side_count`, `opposite_side_count`.

**Pipeline (`civic_tracker`)**
- `stage2_llm.py`: `ExtractedEvent.sides: List[ExtractedSide]` (label, kind) and
  `ExtractedInvolvement.side: Optional[str]`, plus the prompt rules above. `sanitize_result` drops a `side` that
  doesn't match a listed label.
- `resolver.py`: persist the sides and map each involvement's label to its `side_id`. When events merge,
  merge sides with the same label (a fuzzy label match, e.g. "Roe campaign" = "Jane Roe campaign", reusing
  the name compatibility helpers).
- `measures.store_votes`: for a motion with at least one "no" vote, create "aye"/"no" sides on the linked event
  and set `side_id` on the members' involvement rows (creating PARTICIPANT rows for members who voted but
  weren't mentioned).
- `assign_sides.py` + worker command `ASSIGN_SIDES` (admin button and CLI `civic-tracker assign-sides
  [--event N] [--limit N]`). It runs on the local model when idle, with the existing API fallback and daily
  cap, newest events first, and is idempotent (skips events that already have `source='backfill'` sides).
- Re-extracting an article replaces its extraction sides and keeps `manual` ones.

**Website**
- Event page: actors grouped under their side headings (with an "Unaligned" group).
- Coalition panel: "same side in N events · opposite side in M" next to the percentage.
- Admin: on an event, move an involvement to another side (stored as a `manual` side, which survives
  re-extraction). Comes for free with the dispute tooling already there.

## Order of work
1. Migration v2.0 + view, with tests on synthetic events (the three rows above as fixtures). **You apply it.**
2. Roll-call sides (no LLM). Checks the view end-to-end on exact data.
3. Stage 2 schema/prompt + resolver. New articles get sides from then on.
4. `assign-sides` backfill job. Run it on the 1,112 multi-actor events, starting with elections and
   lawsuits.
5. UI: event-page grouping, coalition counts, manual side moves.
6. Mark coalition-affected bios dirty, and regenerate them after the article queue drains (same rule as
   now).

## Cost and time
- Backfill: about 1,100 short prompts (around 1.5k tokens in, 300 out). On the local model that's roughly 2–4
  hours of idle time. On the API at current DeepSeek pricing it's well under $1.
- Stage 2 prompt: about 150 more tokens per article, with no noticeable change in speed.
- Build: 2–3 focused sessions (steps 1–3, then 4, then 5).

## How we'll know it worked
- A hand-labeled check set of about 40 pairs:
  - the CA-4 race (both candidates, both campaign managers, the PACs on each side);
  - a Woodland council vote with dissent;
  - a lawsuit against the City of Davis;
  - a police controversy (critics vs. department).
  Measure the share of pairs with the right sign before and after. Today the rival campaign manager and the Super PAC pairs are wrong.
- Spot checks: no candidate appears as an ally of their opponent's staff; no Super PAC appears as an opponent of
  the candidate it helps.
- Regression: single-question events (an ordinance with supporters and critics) give the same coalition result
  as v1.9.

## Decisions (2026-11-12)
- **Endorsers count.** In multi-candidate races an involvement may sit on more than one side (an endorser of
  two candidates). Two entities are on the "same side" when their side sets overlap, and on "opposite sides"
  when both have sides and the sets don't overlap. So shared endorsers of the same candidate are allies.
  Schema consequence: sides are a many-to-many link (`TB_Entity_Event_Side`), not a single `side_id` column.
- **Backfill runs on the API** (DeepSeek, the burst client) under its own daily cap, not the local model.
