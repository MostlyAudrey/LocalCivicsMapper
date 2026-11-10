# Yolo County government records: where they are and how we can get them

Surveyed 2026-11-10 (one or two polite requests per site, honest CivicTrackerBot User-Agent).
Related: `/root/PLAN_CITY_RECORDS.md` (cities), `/root/records_requests/` (CPRA / Brown Act letters).

**Priority (user, 2026-11-10):** legislative and executive actions (votes, ordinances, resolutions, adopted
budgets, executive decisions) and commission / committee reports. Grand Jury reports are out of scope.

## 1. Blocked: needs the records request (`records_requests/03_yolo_county_cpra.md`)
| Records | Where | Status |
|---|---|---|
| Board of Supervisors agendas, staff reports, minutes | public.destinyhosted.com/96561 (AgendaQuick) | robots.txt `Disallow: /` for everyone |
| County Planning Commission, other county commissions | same AgendaQuick tenant | same |
| BOS minutes archive, document search, video (2007+) | yolocounty.gov | Akamai bot protection (403) |

The county CPRA letter (03) asks for BOS + Planning Commission agendas/packets/minutes, **reports from county
commissions, advisory committees and boards to the BOS, and adopted budgets** (2026-11-10). The standing Brown Act
request (04) covers future BOS/Planning Commission packets by email.

## 2. Reachable now with the new Agenda Center scraper (method `agendacenter`)
| Agency | URL | What's there |
|---|---|---|
| Woodland boards & committees | cityofwoodland.gov/AgendaCenter | 2015-2022 archive (Library Board, Flood Control, Water Utility, Sustainability...). Source #26, inactive |
| Winters commissions & committees | cityofwinters.org/AgendaCenter | 2021+ (Planning Commission 2024+, Natural Resources, Climate Action, Fire District, 2x2s). Source #27, inactive |
| County Elections advisory committees | ace.yolocounty.gov/AgendaCenter | Language / Voting Accessibility Advisory Committees, 2021-2025. Low value; add if wanted |

## 3. Reachable, but each posts PDFs on its own page (needs a generic "PDF links" crawler, see below)
| Agency | Agendas page | Platform / robots |
|---|---|---|
| Yolo LAFCo (annexations, city boundaries, service reviews) | yololafco.org / yololafco.specialdistrict.org (packets since ~2013) | CivicPlus Special District; robots allow |
| Yolo County Office of Education board | ycoe.org/Board/<year>-Board-Meetings/ | static pages; no robots.txt |
| Valley Clean Energy (Davis/Woodland/Winters/County JPA) | valleycleanenergy.org/board-meetings/ , /cac-meetings/ | WordPress; robots allow |
| First 5 Yolo commission | first5yolo.org/about/meetings-minutes | WordPress; packets as PDFs |
| Yolo County Flood Control & Water Conservation District | ycfcwcd.org/agenda | WordPress; robots allow |
| Yolo Habitat Conservancy | yolohabitatconservancy.org/meetings | robots allow |
| Yolo County Housing (housing authority) | ych.ca.gov/about/#agenda | WordPress; robots allow |
| Yolo-Solano Air Quality Management District | ysaqmd.org (board page to locate) | WordPress; robots allow |
| Water Resources Association of Yolo County | yolowra.org (page to locate) | robots allow |
| YoloBus / Yolo County Transportation District | yolobus.com (board page to locate; yctd.org didn't answer) | to check |
| SACOG (regional planning, includes Yolo) | sacog.org | 403 to bots - skip / ask |

Proposed generic method `pdf_links`: listing page(s) -> PDF links matching a pattern (agenda / packet /
minutes) -> date from the link text or file name -> one `official_record` per PDF, history by following
year pages. One small config per agency instead of one scraper each.

## 4. Campaign finance
Yolo County candidates file Form 460s through **eCampaign** (ace.yolocounty.gov/260/eCampaign-Electronic-Filing).
A public search of filed statements wasn't confirmed; Gov. Code § 84615 requires free online public access
when a county adopts e-filing. Next step: find the eCampaign public portal link (or ask the Elections Office,
530-666-8133). The Cities of Davis/Woodland/West Sacramento/Winters have their own filing officers (city
clerks) - check each for NetFile or similar.

## 5. Other county records worth requesting
- Budget documents and CAO reports (yolocounty.gov, blocked) - add to the county CPRA request.
- Sheriff/DA press releases: already crawled (sources #14, #15).
- SB 272 enterprise system catalog (names the agenda/document systems to ask for in the request).
