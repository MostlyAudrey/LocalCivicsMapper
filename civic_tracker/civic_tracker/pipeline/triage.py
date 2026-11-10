"""Triage: decide BEFORE any NLP/LLM work whether a queued article is local civic news worth processing.

Checks, cheapest first:
  1. URL rules      - per-source scrape_config.url_include / url_exclude regexes + global non-news sections
  2. Title rules    - recipes, listicles, horoscopes, puzzles, sponsored content
  3. Genre          - Qwen3 embedding of title + lead vs. prototype descriptions (news, opinion, announcement,
                      lifestyle, sports, entertainment, advertorial). Only news / opinion / announcement pass.
  4. Locality       - weighted Yolo County gazetteer score (title mentions count double). The source's own
                      name ("Davis Vanguard", ...) is removed first so bylines don't count.

Rejected articles become SKIPPED with a skip_reason; every triaged article gets a genre, which later
weighting can use (e.g. opinion pieces count less toward an entity's stances).
An admin "process anyway" sets skip_reason = 'override', which bypasses triage once.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Literal, Optional, Sequence

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from civic_tracker.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- URL + title rules
# Press releases are NOT excluded: government/agency releases (DA, cities, county, UC Davis) are wanted.
# /online_features/ is TownNews' syndicated section (paid national company releases, recipes) - that goes.
GLOBAL_URL_EXCLUDE = [
    r"/online_features/", r"/sponsored", r"/advertorial", r"/brandavestudio/",
    r"/horoscopes?/", r"/puzzles?/", r"/comics?/", r"/obituar", r"/weddings?/", r"/recipes?/",
]
# Wider-area sections: downloaded, but kept only if they name a Yolo place or one of Yolo's representatives
# (has_yolo_angle). Per-source additions: scrape_config.url_require_local.
GLOBAL_URL_REQUIRE_LOCAL = [r"/news/national/", r"/news/world/", r"/news/nation/", r"/ap/", r"/wire/"]
NON_NEWS_TITLE = re.compile(
    r"(^\d+\s+(?:best|easy|ways|things|tips|recipes|dishes|reasons|places|ideas|gifts|foods|meals|snacks|"
    r"desserts|drinks|habits|signs|secrets|movies|books|songs)\b)"
    r"|\b(recipes?|casseroles?|crock[- ]?pot|slow cooker|horoscopes?|crossword|sudoku|wordle|"
    r"sponsored|advertorial|paid content)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------- LLM classifier
KEEP_GENRES = {"news", "opinion", "announcement"}
KEEP_RELEVANCE = {"local", "regional"}
Genre = Literal["news", "opinion", "announcement", "lifestyle", "sports", "entertainment", "advertorial", "other"]


class TriageLabel(BaseModel):
    genre: Genre
    yolo_relevance: Literal["local", "regional", "none"]
    reason: str = Field(max_length=140)


TRIAGE_PROMPT = """You screen articles for a civic-information database about Yolo County, California \
(Davis, Woodland, West Sacramento, Winters, Esparto, Capay Valley, Madison, Knights Landing, Clarksburg, \
Dunnigan, Yolo County government, UC Davis, local school districts, local police, courts and elections).

Classify the article from its title and opening.
genre:
- news: reporting on government, policy, elections, public safety, courts, schools, housing, land use, \
transportation, public health, or community controversies.
- opinion: op-ed, editorial, column, or letter to the editor arguing a position.
- announcement: notice of a public meeting, civic program, service change, award, or nonprofit/community event.
  Official press releases from government agencies, police, prosecutors, courts, schools, or UC Davis are \
news or announcement (never advertorial).
- lifestyle (recipes, travel, wellness, shopping), sports (game results), entertainment (arts, music, \
film, celebrities), advertorial (a company marketing its own products or services; ads), other.
yolo_relevance:
- local: about Yolo County places, institutions, officials, or residents.
- regional: not set in Yolo but directly about Yolo residents' representation or services, e.g. \
California's 4th Congressional District race, state legislators or ballot measures specifically for districts \
containing Yolo, Sacramento-region agencies serving Yolo. National or state stories about the actions, votes, \
bills or statements of Yolo's representatives (U.S. Reps. Mike Tolliver and Doris Mayfield, Assemblymember \
Cecilia Ortega-Lind, State Senator Christopher Caldwell) are regional.
- none: other counties/cities, statewide or national stories without a Yolo-specific angle.
reason: one short sentence. Reply with JSON only."""


class LLMClassifier:
    """Short structured call to the local LLM (thinking off, tiny 3-field schema)."""

    def __init__(self) -> None:
        self.client = AsyncOpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY,
                                  timeout=120, max_retries=1)
        self.schema = TriageLabel.model_json_schema()

    async def classify(self, title: str, lead: str, source_name: str) -> TriageLabel:
        resp = await self.client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[{"role": "system", "content": TRIAGE_PROMPT},
                      {"role": "user", "content": f"Source: {source_name}\nTitle: {title}\n\n{lead[:1500]}"}],
            temperature=0.0, max_tokens=200,
            response_format={"type": "json_schema",
                             "json_schema": {"name": "TriageLabel", "schema": self.schema, "strict": True}},
            extra_body={"chat_template_kwargs": {"thinking": False}},
        )
        content = resp.choices[0].message.content or ""
        return TriageLabel.model_validate(json.loads(content[content.find("{"): content.rfind("}") + 1]))

# ---------------------------------------------------------------------------- locality gazetteer
# weight 2: unambiguous Yolo County names; weight 1: names that are also common surnames/words.
GAZETTEER: Dict[str, int] = {
    r"Yolo": 2, r"West Sacramento": 2, r"West Sac\b": 2, r"Esparto": 2, r"Capay": 2, r"Knights Landing": 2,
    r"Clarksburg": 2, r"Dunnigan": 2, r"Guinda": 2, r"Rumsey": 2, r"Zamora": 1, r"UC Davis": 2, r"UCD\b": 1,
    r"DJUSD": 2, r"Davis Joint Unified": 2, r"Woodland Joint Unified": 2, r"Washington Unified": 2,
    r"Esparto Unified": 2, r"Winters Joint Unified": 2, r"ASUCD": 2, r"Unitrans": 2, r"Putah Creek": 2,
    r"Cache Creek": 2, r"Yocha Dehe": 2, r"Davis City Council": 2, r"City of Davis": 2, r"Woodland City Council": 2,
    r"City of Woodland": 2, r"Winters City Council": 2, r"City of Winters": 2,
    r"Davis(?! Vanguard| Enterprise| Dirt| Political Review)": 1, r"Woodland(?! Hills)": 1,
    r"Winters(?! Express)": 1,
}
_GAZ = [(re.compile(rf"\b{p}"), w) for p, w in GAZETTEER.items()]
MAX_HITS_PER_TERM = 3

# Yolo County's representatives in Congress and the state Legislature: national/state stories about them
# count as Yolo-relevant. UPDATE AFTER ELECTIONS / REDISTRICTING (also named in TRIAGE_PROMPT below).
YOLO_REPRESENTATIVES = {
    "Mike Tolliver": "U.S. Rep., CA-4",
    "Doris Mayfield": "U.S. Rep., CA-7 (West Sacramento)",
    "Cecilia Ortega-Lind": "Assemblymember, AD-4",
    "Christopher Caldwell": "State Senator, SD-3",
}
_REPS = re.compile(r"\b(" + "|".join(re.escape(n) for n in YOLO_REPRESENTATIVES)
                   + r"|Rep\.? Tolliver|Congressman Tolliver|Ortega-Lind|Caldwell|Mayfield)\b")


def has_yolo_angle(title: str, text: str, strip: Sequence[str] = ()) -> bool:
    """Cheap test for wider-area stories: a Yolo place/body or one of Yolo's representatives is named."""
    return locality_score(title, text, strip) >= 2 or bool(_REPS.search(f"{title or ''}\n{text or ''}"))


def locality_score(title: str, text: str, strip: Sequence[str] = ()) -> int:
    """Weighted count of Yolo place/body mentions; title hits count double; capped per term."""
    body = text or ""
    for s in strip:
        if s:
            body = body.replace(s, " ")
            title = (title or "").replace(s, " ")
    score = 0
    for rx, w in _GAZ:
        hits = min(len(rx.findall(body)), MAX_HITS_PER_TERM) + 2 * min(len(rx.findall(title or "")), 1)
        score += w * hits
    return score


@dataclass
class TriageResult:
    keep: bool
    genre: Optional[str] = None
    locality: Optional[int] = None          # gazetteer score (diagnostic)
    relevance: Optional[str] = None         # local | regional | none (LLM)
    reason: Optional[str] = None
    detail: Dict[str, str] = field(default_factory=dict)


class Triage:
    """URL rules -> title rules -> LLM label. Fails OPEN: if the classifier errors, the article is kept
    (it then costs a full extraction, but nothing is silently lost)."""

    def __init__(self, classifier: Optional[LLMClassifier] = None) -> None:
        self.classifier = classifier or LLMClassifier()

    async def check(self, *, url: str, title: str, text: str, source: Dict) -> TriageResult:
        cfg = source.get("scrape_config") or {}
        for rx in (cfg.get("url_exclude") or []) + GLOBAL_URL_EXCLUDE:
            if re.search(rx, url or ""):
                return TriageResult(False, reason=f"url excluded ({rx})")
        includes = cfg.get("url_include") or []
        if includes and not any(re.search(rx, url or "") for rx in includes):
            return TriageResult(False, reason="url outside the source's news sections")
        if NON_NEWS_TITLE.search(title or ""):
            return TriageResult(False, genre="lifestyle", reason="non-news title")

        strip = [source.get("name", "")] + (cfg.get("locality_strip") or [])
        # wider-area sections (e.g. /news/national/): keep only stories naming a Yolo place or representative
        if any(re.search(rx, url or "") for rx in (cfg.get("url_require_local") or []) + GLOBAL_URL_REQUIRE_LOCAL) \
                and not has_yolo_angle(title, text, strip):
            return TriageResult(False, reason="wider-area section without a Yolo place or representative")

        loc = locality_score(title, text, strip=strip)
        try:
            label = await self.classifier.classify(title or "", text or "", source.get("name", ""))
        except Exception as exc:  # noqa: BLE001 - fail open
            log.warning("triage classifier failed (%s); keeping article", exc)
            return TriageResult(True, locality=loc, reason=f"classifier error, kept: {type(exc).__name__}")
        detail = {"llm_reason": label.reason}
        if label.genre not in KEEP_GENRES:
            return TriageResult(False, genre=label.genre, locality=loc, relevance=label.yolo_relevance,
                                reason=f"not news ({label.genre}): {label.reason}", detail=detail)
        assume_local = cfg.get("locality") == "assume"
        if label.yolo_relevance not in KEEP_RELEVANCE and not assume_local:
            return TriageResult(False, genre=label.genre, locality=loc, relevance=label.yolo_relevance,
                                reason=f"not local: {label.reason}", detail=detail)
        return TriageResult(True, genre=label.genre, locality=loc, relevance=label.yolo_relevance, detail=detail)


def url_allowed(url: str, cfg: Dict) -> bool:
    """Crawler-side section filter: same URL rules as triage, applied before downloading."""
    if any(re.search(rx, url or "") for rx in (cfg.get("url_exclude") or []) + GLOBAL_URL_EXCLUDE):
        return False
    includes = cfg.get("url_include") or []
    return not includes or any(re.search(rx, url or "") for rx in includes)
