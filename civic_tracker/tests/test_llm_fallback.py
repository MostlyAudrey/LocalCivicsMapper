"""Truncated local outputs go to the API model; over-long fields are trimmed instead of failing the article."""
import json

import pytest

from civic_tracker.pipeline.stage2_llm import (ArticleExtractionResult, OutputTruncated, TruncationFallbackExtractor,
                                               parse_extraction)

ASYNC = pytest.mark.asyncio(loop_scope="session")
OK = ArticleExtractionResult(events=[], affiliations=[])


class FakeExtractor:
    def __init__(self, name, fail_times=0, cost=0.01):
        self.provider = self.model = name
        self.fail_times, self.cost = fail_times, cost
        self.calls = 0
        self.spent_usd = 0.0

    async def extract(self, title, *_a, **_kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OutputTruncated("LLM output truncated at max_tokens=32000")
        self.spent_usd += self.cost
        return OK


@ASYNC
async def test_truncated_local_output_goes_to_the_api():
    local, api = FakeExtractor("local", fail_times=1), FakeExtractor("api")
    ex = TruncationFallbackExtractor(local, lambda: api)
    assert await ex.extract("Council minutes", None, [], "") is OK
    assert (local.calls, api.calls, ex.fallbacks) == (1, 1, 1)
    assert ex.model == "local"  # attributes come from the primary
    # the next article still starts on the local model
    await ex.extract("Next", None, [], "")
    assert (local.calls, api.calls) == (2, 1)


@ASYNC
async def test_no_api_or_cap_reached_retries_locally():
    local = FakeExtractor("local", fail_times=1)
    assert await TruncationFallbackExtractor(local, None).extract("t", None, [], "") is OK
    assert local.calls == 2

    def no_key():
        raise RuntimeError("BURST_LLM_API_KEY is not set")
    local = FakeExtractor("local", fail_times=1)
    assert await TruncationFallbackExtractor(local, no_key).extract("t", None, [], "") is OK
    assert local.calls == 2

    local, api = FakeExtractor("local", fail_times=99), FakeExtractor("api", cost=1.0)
    ex = TruncationFallbackExtractor(local, lambda: api, daily_cap_usd=1.0)
    await ex.extract("first", None, [], "")            # API used: $1 spent, cap reached
    with pytest.raises(OutputTruncated):
        await ex.extract("second", None, [], "")       # cap: local retry only (still loops here)
    assert api.calls == 1


def test_overlong_fields_are_trimmed_not_rejected():
    motion = "Hold a public hearing and adopt Resolution No. 8650. " * 12  # ~650 chars, limit 400
    payload = {"events": [{
        "canonical_title": "Council adopts resolution", "event_type": "LEGISLATIVE_VOTE",
        "summary": "The council voted.", "topic_tags": [],
        "involvements": [{"entity_name": "Woodland City Council", "entity_type": "GOVERNMENT_BODY",
                          "role_in_event": "voting body", "stance": "SUPPORT", "action_summary": "x " * 400,
                          "direct_quotes": []}],
        "votes": [{"motion": motion, "outcome": "PASSED",
                   "measure": {"title": "Resolution 8650", "measure_type": "RESOLUTION"}}]}],
        "affiliations": []}
    r = parse_extraction(json.dumps(payload))
    ev = r.events[0]
    assert len(ev.votes[0].motion) <= 400 and ev.votes[0].motion.endswith("…")
    assert ev.votes[0].motion.startswith("Hold a public hearing")
    assert len(ev.involvements[0].action_summary) <= 600
