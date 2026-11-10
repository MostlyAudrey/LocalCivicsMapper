"""Wider-area sections (url_require_local): keep national/state stories only with a Yolo angle."""
import asyncio

from civic_tracker.pipeline.triage import Triage, TriageLabel, has_yolo_angle


class _FakeClassifier:
    def __init__(self):
        self.calls = 0

    async def classify(self, title, lead, source_name):
        self.calls += 1
        return TriageLabel(genre="news", yolo_relevance="regional", reason="about Yolo's representative")


SRC = {"name": "Winters Express", "scrape_config": {}}  # /news/national/ is a global require-local section
NATIONAL = "https://www.wintersexpress.com/news/national/some-story/article_1.html"


def test_has_yolo_angle():
    assert has_yolo_angle("House passes wildfire bill", "Rep. Mike Tolliver, D-St. Helena, said ...")
    assert has_yolo_angle("Bill advances", "Assemblymember Cecilia Ortega-Lind's measure would ...")
    assert has_yolo_angle("Flood funding", "The money goes to West Sacramento levees.")
    assert not has_yolo_angle("Supreme Court to hear migrant worker dispute", "WASHINGTON (AP) - The justices ...")
    # the paper's own name is stripped before scoring, so its masthead doesn't count as a Yolo mention
    assert not has_yolo_angle("Healthcare tops priorities", "Winters Express wire report from Washington",
                              strip=["Winters Express"])


def test_national_section_requires_yolo_angle():
    clf = _FakeClassifier()
    t = Triage(classifier=clf)
    r = asyncio.run(t.check(url=NATIONAL, title="Supreme Court to hear migrant worker dispute",
                            text="WASHINGTON (AP) - The justices will hear arguments ...", source=SRC))
    assert not r.keep and "Yolo" in r.reason and clf.calls == 0  # rejected before the LLM call
    r = asyncio.run(t.check(url=NATIONAL, title="House passes wildfire funding",
                            text="Rep. Mike Tolliver said the bill helps Napa and Yolo counties ...", source=SRC))
    assert r.keep and clf.calls == 1
    # other sections are unaffected by the rule
    r = asyncio.run(t.check(url="https://www.wintersexpress.com/news/local/x.html", title="Council meets",
                            text="The council met Tuesday.", source=SRC))
    assert r.keep and clf.calls == 2
