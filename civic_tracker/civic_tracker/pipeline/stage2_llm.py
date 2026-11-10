"""Stage 2: DeepSeek V4-flash structured extraction via JSON-Schema guided decoding."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import List, Literal, Optional, Protocol, Tuple

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from civic_tracker.config import settings
from civic_tracker.pipeline.telemetry import llm_call_timer, usage_fields

log = logging.getLogger(__name__)

EntityType = Literal["PERSON", "ORGANIZATION", "GOVERNMENT_BODY", "COALITION"]
StanceType = Literal["SUPPORT", "OPPOSE", "NEUTRAL", "APOLOGETIC", "DEFENSIVE", "INVESTIGATIVE", "PARTICIPANT"]
EventType = Literal[
    "LEGISLATIVE_VOTE", "POLICY_ANNOUNCEMENT", "PUBLIC_CONTROVERSY",
    "LAW_ENFORCEMENT_ACTION", "MEETING", "ELECTION", "LEGAL_ACTION", "OTHER",
]


class ExtractedAffiliation(BaseModel):
    person_name: str = Field(max_length=100, description="Full canonical name of the person without title prefix")
    organization_name: Optional[str] = Field(default=None, max_length=120, description="Canonical name of the organization or body")
    role_title: str = Field(max_length=60, description="Official role or title, e.g., 'Mayor', 'City Manager', 'Police Officer'")


class ExtractedInvolvement(BaseModel):
    entity_name: str = Field(max_length=120, description="Canonical name of the person, agency, or organization")
    entity_type: EntityType
    role_in_event: str = Field(max_length=160, description="Specific role, connection, or purpose in this event")
    stance: StanceType
    action_summary: str = Field(max_length=600, description="1-2 sentences detailing their specific actions, decisions, or statements")
    direct_quotes: List[str] = Field(default_factory=list, description="Exact verbatim quotes attributed to this entity")
    evidence: Literal["reported", "self", "claimed"] = Field(
        default="reported",
        description="reported = the article reports what this entity did/said as fact (news reporting, ideally "
                    "with quotes); self = the entity's own statement (its press release, or the author of an "
                    "opinion piece stating their own position); claimed = a third party (e.g. an op-ed author) "
                    "asserts or characterizes what this entity did, said or believes")
    claimed_by: Optional[str] = Field(default=None, max_length=120,
                                      description="For evidence=claimed: who makes the claim (person or outlet)")


MeasureType = Literal["ORDINANCE", "RESOLUTION", "BUDGET", "CONTRACT", "LAND_USE", "APPOINTMENT", "POLICY",
                      "BALLOT_MEASURE", "OTHER"]
VoteOutcome = Literal["PASSED", "FAILED", "CONTINUED", "TABLED", "WITHDRAWN", "OTHER"]


class ExtractedMeasureRef(BaseModel):
    """A specific measure an event is about (any article type): lets news coverage be linked to the measure."""
    title: str = Field(max_length=200, description="Short name of the measure, e.g. 'Village Farms development "
                                                  "agreement', 'Fiscal Year 2026-27 budget', 'Measure J'")
    measure_type: MeasureType = "OTHER"
    measure_number: Optional[str] = Field(default=None, max_length=60,
                                          description="Official number if stated, e.g. 'Ordinance No. 2654', "
                                                      "'Resolution 26-031'. null if none is stated.")
    body_name: Optional[str] = Field(default=None, max_length=120,
                                     description="The body that acts on it, with its city, e.g. 'Davis City Council'")


class ExtractedVote(BaseModel):
    """One recorded roll call. ONLY from official minutes / records of the meeting itself."""
    measure: ExtractedMeasureRef
    motion: str = Field(max_length=400, description="What was moved, e.g. 'Adopt Ordinance No. 2654 amending ...'")
    outcome: VoteOutcome
    tally: Optional[str] = Field(default=None, max_length=20, description="As recorded, e.g. '4-1' or '5-0'")
    ayes: List[str] = Field(default_factory=list, description="Member names exactly as listed under Ayes/Yes")
    noes: List[str] = Field(default_factory=list, description="Member names listed under Noes/No")
    abstain: List[str] = Field(default_factory=list, description="Member names listed as abstaining")
    absent: List[str] = Field(default_factory=list, description="Member names listed as absent")
    recused: List[str] = Field(default_factory=list, description="Member names who recused themselves")


class ExtractedEvent(BaseModel):
    canonical_title: str = Field(max_length=200, description="Standardized, objective title for the real-world event")
    event_type: EventType
    event_date_iso: Optional[str] = Field(default=None, max_length=10, description="YYYY-MM-DD resolved against article published_at")
    summary: str = Field(max_length=1200, description="2-3 sentence objective summary of the event")
    topic_tags: List[str] = Field(description="2-5 lowercase policy tags, e.g., ['immigration', 'policing', 'civil-rights']")
    involvements: List[ExtractedInvolvement]
    measures: List[ExtractedMeasureRef] = Field(
        default_factory=list, description="Specific ordinances / resolutions / projects / budgets / ballot measures "
                                          "this event is about (empty if none)")
    votes: List[ExtractedVote] = Field(
        default_factory=list, description="Recorded roll-call votes - ONLY for official minutes (see rules)")


class ArticleExtractionResult(BaseModel):
    affiliations: List[ExtractedAffiliation]
    events: List[ExtractedEvent]


SYSTEM_PROMPT = """You are a meticulous civic-news analyst. From the evidence bundle of a local news article, \
extract (1) people's organizational affiliations and (2) the distinct real-world civic events the article \
reports on, with every entity's involvement, stance, actions, and verbatim quotes.

Rules:
- Use ONLY information stated in the evidence. Do not invent people, roles, dates, or quotes.
- Bracketed names such as "She [Gloria Ravel]" are coreference annotations: attribute the statement to the \
bracketed name, and never include the bracket text inside a quote.
- Names: strip titles and honorifics (Mayor, Officer, Councilmember, Chief, Dr., Mr., Ms., etc.) from \
person_name and entity_name and put the title in ExtractedAffiliation.role_title instead. EXCEPTION: when the \
article only gives a title plus surname (e.g. "Officer Pemberton"), keep "Officer Pemberton" as the name.
- Use the fullest form of each name that appears in the article (e.g. "U.S. Immigration and Customs \
Enforcement" rather than "ICE" only if the full form appears; otherwise keep "ICE"). Use the same name string \
for the same entity everywhere in your output.
- entity_type: PERSON for individuals; GOVERNMENT_BODY for public agencies, departments, councils, courts and \
law enforcement; ORGANIZATION for nonprofits, companies, advocacy and activist groups; COALITION for named \
alliances of multiple groups.
- Local government bodies ALWAYS carry their city or county in the name, even when the article only says \
"the council" or "the Planning Commission": write "Woodland Planning Commission", "Davis City Council", \
"West Sacramento Police Department", "Yolo County Board of Supervisors". Work out the city from the article \
(see "Article jurisdiction" for the most likely one, but follow the text when it names another city). \
West Sacramento and Sacramento are different cities. Never output a bare "City Council", "Planning \
Commission", "Police Department" or "Board of Supervisors"; if you truly cannot tell which city's body it is, \
leave that body out.
- Resolve relative dates ("last Wednesday", "yesterday") against the Publication Date and output event_date_iso \
as YYYY-MM-DD. Use null if the event date cannot be determined.
- direct_quotes: copy the words inside quotation marks EXACTLY, character for character, one quote per list \
item. Do not paraphrase, merge separate quotes, add ellipses, or include the coreference brackets. Attribute a \
jointly issued statement to each co-author. Omit quotes you cannot copy exactly.
- event_type, pick the most specific:
  LEGISLATIVE_VOTE = a council/board/legislature vote; POLICY_ANNOUNCEMENT = an official announces or issues a \
policy, directive, ordinance or program; PUBLIC_CONTROVERSY = a disputed incident drawing criticism, apologies, \
or calls for accountability; LAW_ENFORCEMENT_ACTION = an arrest, raid, stop, or citation reported mainly as a \
police action without public dispute; MEETING = a public meeting where no single decision dominates; \
ELECTION = campaigns, candidacies, results; LEGAL_ACTION = lawsuits, charges, court rulings; OTHER otherwise.
- stance is the entity's position toward the event's central action or decision (the thing being disputed, \
decided, or announced), judged by what they did or said:
  SUPPORT = endorses, advocates for, or carries out a policy they champion; OPPOSE = criticizes, protests, \
or resists it; APOLOGETIC = apologizes or expresses regret for it (this takes priority over OPPOSE when an \
official apologizes for their own institution); DEFENSIVE = defends their own or their institution's conduct \
against criticism or denies wrongdoing; INVESTIGATIVE = reviews, investigates, audits, or urges waiting for \
findings; PARTICIPANT = directly took part (e.g. made the stop, conducted the operation) without stating a \
position; NEUTRAL = mentioned or reported without a position.
- topic_tags: 2-5 lowercase, hyphenated policy tags (e.g. "immigration", "policing", "civil-rights").
- measures: when an event is about a specific ordinance, resolution, contract, budget, development project or \
land-use approval, appointment, policy, or ballot measure, list it (title, type, its official number ONLY if \
the text states one, and the acting body with its city). Use the same title for the same measure throughout. \
Leave empty for events that aren't about a specific measure.
- votes: ONLY when the article type is MINUTES (official minutes of the meeting). For each motion with a \
recorded vote, give the measure, the motion text, the outcome, the tally, and the member names exactly as \
listed under Ayes / Noes / Abstain / Absent / Recused. A vote is "unanimous" only with names: if the minutes \
list the members present and say "motion carried unanimously", those present are the ayes; otherwise give the \
outcome and tally with empty name lists. Never infer how someone voted from their comments. For every other \
article type (news, opinion, staff reports, agendas) votes MUST be empty - news reports of votes are not \
recorded here.
- Merge multiple paragraphs about the same incident into ONE event. Only list separate events if they are \
genuinely distinct real-world occurrences.
- SCOPE: only civic events: government, public agencies, policy, elections and campaigns, courts and \
law enforcement, public institutions (schools, universities, student government), community organizing and \
public controversies. Game results, athletic performance, arts reviews, recipes, obituaries, and \
event listings with no civic dimension are NOT civic events. If the article contains no civic events, \
return {"affiliations": [], "events": []}.
- ENTITIES must be specific, named people or named organizations/bodies. Do NOT create entities for \
generic or unnamed groups ("Democrats", "progressive organizations", "corporate super PACs", "opponents", \
"county officials", "residents"), for the article's own reporter/author, or for the publishing news outlet \
itself unless it is an actor in the story. Political parties may appear only as ORGANIZATION when an \
official party body acts (e.g. "Yolo County Democratic Party").
- COALITION is only for a named alliance of multiple groups (e.g. "Yolo County Coalition for Housing"); \
sports teams, student slates and caucuses are not coalitions (use ORGANIZATION, or omit if not civic).
- AFFILIATIONS: only formal, current positions or memberships stated in the article (Mayor, City Manager, \
Councilmember, Police Officer, Executive Director, Board Member, Candidate for <office>, Senator). Do NOT \
record relationships, opinions, endorsements, past life events, or descriptions ("supporter of", \
"colleague", "served in wartime") as affiliations.
- role_title is a SHORT standard title (1-5 words, title case), never a sentence or a list: e.g. "Mayor", \
"City Manager", "ASUCD Senator", "Candidate". Put the body or place in organization_name instead: \
"City Manager of Davis" -> role_title "City Manager", organization_name "City of Davis"; "candidate for \
ASUCD Senate" -> role_title "Candidate", organization_name "ASUCD Senate". Emit one affiliation per \
distinct position; skip former positions.
- evidence: mark how each involvement is known. Use "reported" only when the article itself reports the \
action or statement as fact. When someone's position is described by a critic, supporter or op-ed author, use \
"claimed" and name that person in claimed_by - do NOT turn the claim into the entity's own stance. For "claimed" \
involvements, write the action_summary as attribution ("According to <claimer>, ...") and use stance NEUTRAL \
unless the entity's position is directly quoted.
- OPINION PIECES (op-eds, columns, letters to the editor; the user message says so): the author's own position \
is a real stance (evidence "self"). Everything the author says about other people is "claimed" by the author. \
Do not create events out of the author's argument itself (e.g. "X's misrepresentations"): only create an event \
for a concrete, checkable real-world occurrence the piece refers to (a vote, debate, filing, donation, ruling).
- OFFICIAL RECORDS (city council staff reports / agenda items; the user message says so): the staff who wrote \
or approved the report are authors, not stance-takers - do not give them stances on the item. The report's \
recommendation is the position of the recommending department or commission (ORGANIZATION, e.g. "City of \
Woodland Public Works Department"), evidence "self", stance SUPPORT for what it recommends. Applicants, \
contractors, grantors and other parties the report describes are "reported". The report predates the council \
decision: the event is the agenda item coming before the council (e.g. "Woodland City Council considers \
conditional use permit for ..."), not its outcome, unless the outcome is stated. Do not record council votes \
that the report does not state.
Respond with JSON matching the provided schema only."""


def build_user_prompt(
    title: str,
    published_at: Optional[datetime],
    candidate_entities: List[str],
    evidence_bundle: str,
    genre: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    record_kind: Optional[str] = None,
) -> str:
    pub = published_at.date().isoformat() if published_at else "unknown"
    juris = (f"{jurisdiction} (most likely; bare \"the council\" / \"the commission\" mean {jurisdiction}'s "
             f"unless the text says otherwise)") if jurisdiction else "unclear - take the city from the text"
    cands = "\n".join(f"- {c}" for c in candidate_entities) or "- (none)"
    kind = {"opinion": "OPINION PIECE (op-ed, column or letter to the editor) - apply the opinion rules",
            "announcement": "announcement / press release", "news": "news report",
            "official_record": "OFFICIAL RECORD (city council agenda item / staff report) - apply the "
                               "official-record rules"}.get(genre or "", "unknown")
    if genre == "official_record" and record_kind == "minutes":
        kind = ("MINUTES (official minutes of the meeting) - record every roll-call vote under `votes`, and link "
                "each event to its measure")
    return (
        f"Article Title: {title}\n"
        f"Article type: {kind}\n"
        f"Article jurisdiction: {juris}\n"
        f"Publication Date (ISO): {pub}\n\n"
        f"GLiNER Candidate Entities:\n{cands}\n\n"
        f"Evidence Bundle:\n<<<\n{evidence_bundle}\n>>>"
    )


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def parse_extraction(content: str) -> ArticleExtractionResult:
    text = _THINK_RE.sub("", content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"LLM response contained no JSON object: {content[:200]!r}")
    return ArticleExtractionResult.model_validate(json.loads(text[start : end + 1]))


_BRACKET_ANNOTATION = re.compile(r"\s?\[[^\]]{1,80}\]")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
_WORD = re.compile(r"[0-9A-Za-zÀ-ɏ]+(?:['’][0-9A-Za-z]+)*")
_OPEN_QUOTES = "\"“"
_CLOSE_QUOTES = "\"”"

QUOTE_MATCH_THRESHOLD = 0.80  # token-level similarity needed to accept a loose match
_SNAP_CHARS = 40              # how far to look for enclosing quotation marks


def _tokens(text: str) -> List[Tuple[str, int, int]]:
    """(normalised word, start, end) for each word; apostrophes unified, lowercased."""
    return [(m.group().replace("’", "'").lower(), m.start(), m.end()) for m in _WORD.finditer(text)]


def _clean_quote(q: str) -> str:
    q = _BRACKET_ANNOTATION.sub("", q)
    return " ".join(q.split()).strip(" \"'“”‘’")


def _snap_to_quote_marks(text: str, start: int, end: int) -> Tuple[int, int]:
    """Widen [start, end) to the enclosing quotation marks if they are close by, so a match that
    clipped or overran a word or two returns the whole quoted span."""
    left = text.rfind("\n", 0, start) + 1
    window = text[max(left, start - _SNAP_CHARS):start]
    opens = [i for i, ch in enumerate(window) if ch in _OPEN_QUOTES]
    right_limit = text.find("\n", end)
    right_limit = len(text) if right_limit == -1 else right_limit
    tail = text[end:min(right_limit, end + _SNAP_CHARS)]
    closes = [i for i, ch in enumerate(tail) if ch in _CLOSE_QUOTES]
    if opens and closes:
        new_start = max(left, start - _SNAP_CHARS) + opens[-1] + 1
        new_end = end + closes[0]
        # strip a trailing comma the article puts before the closing mark ("...," she said)
        span = text[new_start:new_end].rstrip(", ")
        return new_start, new_start + len(span)
    return start, end


def locate_quote(quote: str, source_text: str, src_tokens: Optional[List[Tuple[str, int, int]]] = None) -> Optional[str]:
    """Find the passage in `source_text` that `quote` was taken from and return it verbatim.

    Matching is word-level (case, punctuation, curly quotes, dashes and coreference brackets are
    ignored), so small LLM slips such as a changed word, dropped filler or altered punctuation still
    resolve to the article's exact wording. Returns None if nothing reaches QUOTE_MATCH_THRESHOLD.
    """
    q = [t[0] for t in _tokens(_clean_quote(quote))]
    if len(q) < 2:
        return None
    src = src_tokens if src_tokens is not None else _tokens(source_text)
    words = [t[0] for t in src]
    if not words:
        return None

    anchor = SequenceMatcher(None, words, q, autojunk=False).find_longest_match(0, len(words), 0, len(q))
    if anchor.size < min(3, len(q)):
        return None
    approx = anchor.a - anchor.b
    n = len(q)
    slack = max(2, n // 5)
    best: Tuple[float, int, int] = (0.0, 0, 0)
    for start in range(max(0, approx - slack), min(len(words), approx + slack + 1)):
        for length in range(max(1, n - slack), n + slack + 1):
            end = start + length
            if end > len(words):
                break
            ratio = SequenceMatcher(None, q, words[start:end], autojunk=False).ratio()
            if ratio > best[0]:
                best = (ratio, start, end)
    ratio, s, e = best
    if ratio < QUOTE_MATCH_THRESHOLD:
        return None
    cs, ce = _snap_to_quote_marks(source_text, src[s][1], src[e - 1][2])
    return " ".join(source_text[cs:ce].split()).strip(" \"“”")


def enforce_verbatim_quotes(result: ArticleExtractionResult, source_text: str) -> tuple[ArticleExtractionResult, int]:
    """Replace every extracted quote with the article's exact wording (loose word-level match),
    dropping only quotes that can't be located. Ellipsis-joined quotes are matched per fragment.
    Returns (cleaned result, number of quotes dropped)."""
    src_tokens = _tokens(source_text)
    dropped = 0
    for ev in result.events:
        for inv in ev.involvements:
            kept: List[str] = []
            for q in inv.direct_quotes:
                frags = [f for f in _ELLIPSIS.split(_clean_quote(q)) if len(f.split()) >= 2] or [q]
                located = [locate_quote(f, source_text, src_tokens) for f in frags]
                if all(located):
                    for verbatim in located:
                        if verbatim not in kept:
                            kept.append(verbatim)  # type: ignore[arg-type]
                else:
                    dropped += 1
            inv.direct_quotes = kept
    return result, dropped


_REASONING_LEAK = re.compile(r"\?|\b(?:actually|the article says|instructions|i'll|i will|but the)\b", re.IGNORECASE)


def _looks_like_label(value: Optional[str], max_words: int) -> bool:
    if not value:
        return True
    return len(value.split()) <= max_words and not _REASONING_LEAK.search(value)


def sanitize_result(result: ArticleExtractionResult) -> tuple[ArticleExtractionResult, int]:
    """Drop affiliations / involvements whose short label fields contain sentence-like text
    (the model 'thinking' inside a JSON string). Returns (result, number of items dropped)."""
    dropped = 0
    affs = []
    for a in result.affiliations:
        if (_looks_like_label(a.person_name, 8) and _looks_like_label(a.role_title, 6)
                and _looks_like_label(a.organization_name, 12)):
            affs.append(a)
        else:
            dropped += 1
    result.affiliations = affs
    for ev in result.events:
        keep = []
        for inv in ev.involvements:
            if _looks_like_label(inv.entity_name, 12):
                keep.append(inv)
            else:
                dropped += 1
        ev.involvements = keep
    return result, dropped


class Extractor(Protocol):
    async def extract(
        self, title: str, published_at: Optional[datetime], candidate_entities: List[str], evidence_bundle: str,
        on_call=None, genre: Optional[str] = None, jurisdiction: Optional[str] = None,
        record_kind: Optional[str] = None,
    ) -> ArticleExtractionResult: ...


JSON_OBJECT_INSTRUCTIONS = (
    "\n\nOutput format: reply with ONE json object (no prose, no code fences) that validates against "
    "this JSON Schema:\n{schema}"
)


class DeepSeekExtractor:
    """OpenAI-compatible extraction client. Default: the local vLLM/SGLang server (strict json_schema guided
    decoding). `DeepSeekExtractor.burst()` builds the paid-API variant used by burst mode."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: str = "local",
        response_format: str = "json_schema",       # "json_schema" (strict) | "json_object" (JSON mode)
        extra_body: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        price_in_per_m: float = 0.0,
        price_out_per_m: float = 0.0,
        thinking: Optional[bool] = None,
    ) -> None:
        self.client = AsyncOpenAI(
            base_url=base_url or settings.LLM_BASE_URL,
            api_key=api_key or settings.LLM_API_KEY,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=1,  # a timed-out 12-minute generation shouldn't be silently repeated twice
        )
        self.model = model or settings.LLM_MODEL
        self.provider = provider
        self.response_format = response_format
        self.thinking = settings.LLM_THINKING if thinking is None else thinking
        self.extra_body = ({"chat_template_kwargs": {"thinking": self.thinking}} if extra_body is None
                           else extra_body)
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self.price_in = price_in_per_m
        self.price_out = price_out_per_m
        self.spent_usd = 0.0  # running total for this instance (burst spend cap)
        self.schema = ArticleExtractionResult.model_json_schema()
        self.system_prompt = SYSTEM_PROMPT
        if response_format == "json_object":
            self.system_prompt += JSON_OBJECT_INSTRUCTIONS.format(schema=json.dumps(self.schema))

    @classmethod
    def burst(cls) -> "DeepSeekExtractor":
        if not settings.BURST_LLM_API_KEY:
            raise RuntimeError("BURST_LLM_API_KEY is not set in civic_tracker/.env")
        return cls(
            base_url=settings.BURST_LLM_BASE_URL, api_key=settings.BURST_LLM_API_KEY,
            model=settings.BURST_LLM_MODEL, provider=settings.BURST_PROVIDER_NAME,
            response_format=settings.BURST_RESPONSE_FORMAT, extra_body=json.loads(settings.BURST_EXTRA_BODY or "{}"),
            max_tokens=settings.BURST_MAX_TOKENS, price_in_per_m=settings.BURST_PRICE_INPUT_PER_M,
            price_out_per_m=settings.BURST_PRICE_OUTPUT_PER_M, thinking=settings.BURST_THINKING,
        )

    def cost_of(self, meta: dict) -> float:
        """Conservative: every prompt token at the full (cache-miss) input price."""
        return ((meta.get("prompt_tokens") or 0) * self.price_in
                + (meta.get("completion_tokens") or 0) * self.price_out) / 1_000_000

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, max=30),
        retry=retry_if_exception_type((ValidationError, ValueError, json.JSONDecodeError)),
        reraise=True,
    )
    async def _call(self, user_prompt: str, on_call=None) -> ArticleExtractionResult:
        """One LLM request (retried by tenacity). `on_call(timer, meta, status, error)` (async) receives the
        wall time, concurrency and token usage of EVERY attempt, including failures, for TB_LLM_Call."""
        meta: dict = {"model": self.model, "thinking": self.thinking, "provider": self.provider}
        status, error = "OK", None
        try:
            with llm_call_timer(self.provider) as timer:
                try:
                    return await self._request(user_prompt, meta)
                except asyncio.CancelledError:
                    status, error = "CANCELLED", "cancelled"
                    raise
                except Exception as exc:
                    status = "TRUNCATED" if meta.get("finish_reason") == "length" else "ERROR"
                    error = f"{type(exc).__name__}: {exc}"
                    raise
        finally:
            if on_call is not None:
                try:
                    await on_call(timer, meta, status, error)
                except Exception:  # noqa: BLE001 - telemetry must never break extraction
                    log.exception("LLM telemetry callback failed")

    async def _request(self, user_prompt: str, meta: dict) -> ArticleExtractionResult:
        if self.response_format == "json_object":
            response_format: dict = {"type": "json_object"}  # JSON mode: schema is in the system prompt
        else:  # vLLM / SGLang guided decoding against the Pydantic JSON Schema.
            response_format = {"type": "json_schema",
                               "json_schema": {"name": "ArticleExtractionResult", "schema": self.schema, "strict": True}}
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=self.max_tokens,
            response_format=response_format,
            extra_body=self.extra_body or None,
        )
        meta.update(usage_fields(resp))
        meta["cost_usd"] = self.cost_of(meta)
        self.spent_usd += meta["cost_usd"]
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise ValueError(f"LLM output truncated at max_tokens={self.max_tokens}")
        return parse_extraction(choice.message.content or "")

    async def extract(
        self, title: str, published_at: Optional[datetime], candidate_entities: List[str], evidence_bundle: str,
        on_call=None, genre: Optional[str] = None, jurisdiction: Optional[str] = None,
        record_kind: Optional[str] = None,
    ) -> ArticleExtractionResult:
        prompt = build_user_prompt(title, published_at, candidate_entities, evidence_bundle, genre, jurisdiction,
                                   record_kind)
        return await self._call(prompt, on_call)
