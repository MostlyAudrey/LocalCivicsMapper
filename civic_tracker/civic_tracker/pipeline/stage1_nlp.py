"""Stage 1: fastcoref pronoun resolution + GLiNER zero-shot entity spans + spaCy sentence bundling.

The heavy models are loaded lazily, once per process, and are not thread-safe, so all model
calls are serialised behind a lock and pushed off the event loop with `asyncio.to_thread`.
Each step is injectable (`coref_fn`, `ner_fn`, `sentence_fn`) so tests can run without torch.
"""
from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from civic_tracker.config import settings

GLINER_LABELS: List[str] = [
    "person",
    "public official",
    "law enforcement officer",
    "government agency",
    "activist organization",
    "city council",
    "coalition",
]

PRONOUNS = {
    "he", "she", "they", "his", "her", "hers", "him", "them", "their", "theirs",
    "himself", "herself", "themselves",
}
_LEADING_DETERMINERS = ("the ", "a ", "an ")

Span = Tuple[int, int]
# coref_fn(text) -> clusters of (start, end) char spans
CorefFn = Callable[[str], List[List[Span]]]
# ner_fn(text, labels) -> [{"start", "end", "text", "label", "score"}]
NerFn = Callable[[str, Sequence[str]], List[Dict]]
# sentence_fn(text) -> [(start, end)] char spans
SentenceFn = Callable[[str], List[Span]]


@dataclass
class Stage1Result:
    resolved_text: str
    evidence_bundle: str
    candidate_entities: List[str]
    entities: List[Dict] = field(default_factory=list)

    @property
    def skipped(self) -> bool:
        return not self.entities


# ---------------------------------------------------------------------------
# Coreference annotation
# ---------------------------------------------------------------------------
_POSSESSIVE = re.compile(r"[\'\u2019]s?$")


def _strip_possessive(mention: str) -> str:
    return _POSSESSIVE.sub("", mention.strip())


def _is_proper_mention(mention: str) -> bool:
    m = _strip_possessive(mention)
    if not m or m.lower() in PRONOUNS:
        return False
    low = m.lower()
    for det in _LEADING_DETERMINERS:
        if low.startswith(det):
            m = m[len(det):]
            break
    words = [w for w in re.split(r"\s+", m) if w]
    # Proper noun phrase: every alphabetic word that isn't a short connector is capitalised.
    connectors = {"of", "and", "for", "the", "de", "la", "&"}
    alpha = [w for w in words if w[0].isalpha() and w.lower() not in connectors]
    return bool(alpha) and all(w[0].isupper() for w in alpha)


def annotate_coreferences(text: str, clusters: List[List[Span]]) -> str:
    """Insert `[Head Name]` after each pronoun whose cluster has a proper-noun head."""
    inserts: List[Tuple[int, str]] = []
    for cluster in clusters:
        mentions = [(s, e, text[s:e]) for s, e in cluster]
        proper = [(s, e, _strip_possessive(m)) for s, e, m in mentions if _is_proper_mention(m)]
        if not proper:
            continue
        # Longest proper mention is the most informative head ("Gloria Ravel" over "Ravel").
        head = max(proper, key=lambda m: (len(m[2].split()), len(m[2])))[2].strip()
        for s, e, mention in mentions:
            if mention.strip().lower() in PRONOUNS:
                inserts.append((e, f" [{head}]"))
    out = text
    for pos, ins in sorted(set(inserts), key=lambda x: x[0], reverse=True):
        out = out[:pos] + ins + out[pos:]
    return out


# ---------------------------------------------------------------------------
# Evidence bundling
# ---------------------------------------------------------------------------
def build_evidence_bundle(text: str, sentences: List[Span], entities: List[Dict], window: int = 1) -> str:
    """Keep every sentence overlapping an entity span plus `window` neighbours either side."""
    keep: set[int] = set()
    for ent in entities:
        for i, (s, e) in enumerate(sentences):
            if ent["start"] < e and ent["end"] > s:
                keep.update(range(max(0, i - window), min(len(sentences), i + window + 1)))
    if not keep:
        return ""
    parts: List[str] = []
    prev = None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            parts.append("[...]")
        s, e = sentences[i]
        parts.append(" ".join(text[s:e].split()))
        prev = i
    return "\n".join(parts)


def dedupe_candidates(entities: List[Dict]) -> List[str]:
    """Return 'Name (label)' strings, case-insensitively unique by name, highest score wins."""
    best: Dict[str, Dict] = {}
    for ent in entities:
        name = " ".join(ent["text"].split()).strip(" .,;:'\"")
        if not name:
            continue
        key = name.lower()
        if key not in best or ent.get("score", 0) > best[key].get("score", 0):
            best[key] = {**ent, "text": name}
    ordered = sorted(best.values(), key=lambda e: e["start"])
    return [f"{e['text']} ({e['label']})" for e in ordered]


def _chunk_by_sentences(sentences: List[Span], max_chars: int = 1200) -> List[Span]:
    chunks: List[Span] = []
    cur_start: Optional[int] = None
    cur_end = 0
    for s, e in sentences:
        if cur_start is None:
            cur_start, cur_end = s, e
        elif e - cur_start > max_chars:
            chunks.append((cur_start, cur_end))
            cur_start, cur_end = s, e
        else:
            cur_end = e
    if cur_start is not None:
        chunks.append((cur_start, cur_end))
    return chunks


# ---------------------------------------------------------------------------
# Default model-backed implementations (lazy singletons)
# ---------------------------------------------------------------------------
_model_lock = threading.Lock()
_coref_model = None
_gliner_model = None
_spacy_nlp = None


def _spacy_sentences(text: str) -> List[Span]:
    global _spacy_nlp
    if _spacy_nlp is None:
        import spacy

        try:
            _spacy_nlp = spacy.load(settings.SPACY_MODEL, disable=["ner", "lemmatizer"])
        except OSError:
            # Model not downloaded: fall back to the rule-based sentencizer.
            _spacy_nlp = spacy.blank("en")
            _spacy_nlp.add_pipe("sentencizer")
        _spacy_nlp.max_length = max(_spacy_nlp.max_length, 2_000_000)
    doc = _spacy_nlp(text)
    return [(s.start_char, s.end_char) for s in doc.sents if s.text.strip()]


def _fastcoref_clusters(text: str) -> List[List[Span]]:
    global _coref_model
    if _coref_model is None:
        from fastcoref import FCoref

        _coref_model = FCoref(device=settings.COREF_DEVICE)
    preds = _coref_model.predict(texts=[text])
    return [[(int(s), int(e)) for s, e in cluster] for cluster in preds[0].get_clusters(as_strings=False)]


def _gliner_entities(text: str, labels: Sequence[str]) -> List[Dict]:
    global _gliner_model
    if _gliner_model is None:
        from gliner import GLiNER

        _gliner_model = GLiNER.from_pretrained(settings.GLINER_MODEL)
        try:
            _gliner_model = _gliner_model.to(settings.GLINER_DEVICE)
        except Exception:  # pragma: no cover - device move is best-effort
            pass
    # GLiNER has a ~384-token window: run per sentence-aligned chunk and re-offset spans.
    out: List[Dict] = []
    for cs, ce in _chunk_by_sentences(_spacy_sentences(text), settings.GLINER_CHUNK_CHARS):
        chunk = text[cs:ce]
        for ent in _gliner_model.predict_entities(chunk, list(labels), threshold=settings.GLINER_THRESHOLD):
            out.append(
                {
                    "start": cs + int(ent["start"]),
                    "end": cs + int(ent["end"]),
                    "text": ent["text"],
                    "label": ent["label"],
                    "score": float(ent.get("score", 1.0)),
                }
            )
    return out


class Stage1Processor:
    def __init__(
        self,
        coref_fn: Optional[CorefFn] = None,
        ner_fn: Optional[NerFn] = None,
        sentence_fn: Optional[SentenceFn] = None,
        labels: Sequence[str] = GLINER_LABELS,
    ) -> None:
        self.coref_fn = coref_fn or _fastcoref_clusters
        self.ner_fn = ner_fn or _gliner_entities
        self.sentence_fn = sentence_fn or _spacy_sentences
        self.labels = list(labels)

    def process(self, full_text: str) -> Stage1Result:
        with _model_lock:
            clusters = self.coref_fn(full_text)
            resolved = annotate_coreferences(full_text, clusters)
            entities = self.ner_fn(resolved, self.labels)
            if not entities:
                return Stage1Result(resolved_text=resolved, evidence_bundle="", candidate_entities=[], entities=[])
            sentences = self.sentence_fn(resolved)
        bundle = build_evidence_bundle(resolved, sentences, entities)
        return Stage1Result(
            resolved_text=resolved,
            evidence_bundle=bundle,
            candidate_entities=dedupe_candidates(entities),
            entities=entities,
        )

    async def aprocess(self, full_text: str) -> Stage1Result:
        return await asyncio.to_thread(self.process, full_text)
