"""Long texts are coref-resolved in bounded chunks (a 40k-char PDF OOM-killed the worker)."""
from civic_tracker.pipeline import stage1_nlp
from civic_tracker.pipeline.stage1_nlp import _cap_spans


def test_cap_spans_splits_oversized_span_at_whitespace():
    text = ("word " * 100).strip()  # one "sentence" of 499 chars
    spans = _cap_spans(text, [(0, len(text))], 120)
    assert all(e - s <= 120 for s, e in spans)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))  # contiguous, nothing dropped
    assert all(text[e] == " " for _, e in spans[:-1])  # cut at whitespace


def test_cap_spans_leaves_small_spans_alone():
    assert _cap_spans("abc def", [(0, 3), (4, 7)], 10) == [(0, 3), (4, 7)]


def test_fastcoref_chunks_and_reoffsets(monkeypatch):
    calls = []

    class FakeModel:
        def predict(self, texts):
            calls.append(texts)
            chunk = texts[0]

            class P:
                def get_clusters(self, as_strings=False):
                    i = chunk.find("She")
                    return [[(0, 4), (i, i + 3)]] if i >= 0 else []
            return [P()]

    sent = "Mary went home. She slept. "
    text = sent * 20
    monkeypatch.setattr(stage1_nlp, "_coref_model", FakeModel())
    monkeypatch.setattr(stage1_nlp.settings, "COREF_CHUNK_CHARS", 100)
    monkeypatch.setattr(stage1_nlp, "_spacy_sentences",
                        lambda t: [(m, m + len(sent) - 1) for m in range(0, len(t), len(sent))])
    clusters = stage1_nlp._fastcoref_clusters(text)
    assert len(calls) > 1 and all(len(c) == 1 and len(c[0]) <= 100 for c in calls)
    for cluster in clusters:
        assert text[cluster[0][0]:cluster[0][1]] == "Mary"
        assert text[cluster[1][0]:cluster[1][1]] == "She"
