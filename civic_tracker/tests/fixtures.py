"""Test fixtures for the Davis Vanguard ICE-stop scenario.

ARTICLE_1 is the real article (WordPress post 272226), fetched via the site's public
wp-json REST API and stored in tests/data/ (site footer removed).

ARTICLE_2 is a SYNTHETIC follow-up written for the dedup test: it only restates facts from
article 1 and contains no quotes. MOCK_RESULT_* are hand-written Stage 2 outputs for
offline mode; quotes in MOCK_RESULT_1 are verbatim from article 1.

Note: the article spells the officer "Pemberton" (SPEC.MD says "Pemberton").
"""
from datetime import datetime, timezone
from pathlib import Path

from civic_tracker.pipeline.stage2_llm import (
    ArticleExtractionResult,
    ExtractedAffiliation,
    ExtractedEvent,
    ExtractedInvolvement,
)

_DATA = Path(__file__).parent / "data"

ARTICLE_1_URL = "https://news.example/2026/11/davis-mayor-apologizes-observer-stop/"
ARTICLE_1_TITLE = "Davis Mayor and Vice Mayor Apologize Following Police Citation of ICE Observer"
ARTICLE_1_PUBLISHED = datetime(2026, 11, 7, 12, 15, tzinfo=timezone.utc)  # date_gmt from wp-json
ARTICLE_1_TEXT = (_DATA / "davis_mayor_apologizes_ice_stop.txt").read_text(encoding="utf-8")

ARTICLE_2_URL = "https://davisvanguard.org/2026/11/test-followup-davis-ice-observer-citation/"
ARTICLE_2_TITLE = "Davis Review of ICE Observer Citation Continues"
ARTICLE_2_PUBLISHED = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)  # 5 days later
ARTICLE_2_TEXT = (
    "DAVIS, Calif. — The City of Davis is continuing its administrative review of last week's traffic stop "
    "in which Officer Pemberton of the Davis Police Department cited Robin Hale, a community member monitoring "
    "ICE activity with Valley Watch Network, for improper horn use near Cowell Boulevard and Lillard Drive. "
    "Mayor Helen Marsh and Vice Mayor Gloria Ravel apologized for the incident last Friday. "
    "City Manager Owen Brackett has asked residents to let the review run its course. "
    "The Davis Police Department has said it did not assist federal immigration enforcement. "
    "Hale has alleged the stop let ICE agents drive away. "
    "In Sacramento, City Manager Lena Okafor previously barred police from acting as security for ICE."
)

ALL_9 = [
    ("Helen Marsh", "PERSON"),
    ("Gloria Ravel", "PERSON"),
    ("Robin Hale", "PERSON"),
    ("Valley Watch Network", "ORGANIZATION"),
    ("ICE", "GOVERNMENT_BODY"),
    ("Officer Pemberton", "PERSON"),
    ("Davis Police Department", "GOVERNMENT_BODY"),
    ("Owen Brackett", "PERSON"),
    ("Lena Okafor", "PERSON"),
]


def _inv(name, etype, role, stance, summary, quotes=()):
    return ExtractedInvolvement(entity_name=name, entity_type=etype, role_in_event=role, stance=stance,
                                action_summary=summary, direct_quotes=list(quotes))


_DAVIS_TITLE = "Davis Police Officer Cites ICE Observer Robin Hale During Federal Operation; Mayor and Vice Mayor Apologize"

MOCK_RESULT_1 = ArticleExtractionResult(
    affiliations=[
        ExtractedAffiliation(person_name="Helen Marsh", organization_name="City of Davis", role_title="Mayor"),
        ExtractedAffiliation(person_name="Gloria Ravel", organization_name="City of Davis", role_title="Vice Mayor"),
        ExtractedAffiliation(person_name="Officer Pemberton", organization_name="Davis Police Department", role_title="Police Officer"),
        ExtractedAffiliation(person_name="Owen Brackett", organization_name="City of Davis", role_title="City Manager"),
        ExtractedAffiliation(person_name="Lena Okafor", organization_name="City of Sacramento", role_title="City Manager"),
    ],
    events=[
        ExtractedEvent(
            canonical_title=_DAVIS_TITLE,
            event_type="PUBLIC_CONTROVERSY",
            event_date_iso="2026-11-05",
            summary=(
                "A Davis police officer cited Robin Hale for improper horn use while she followed a vehicle she "
                "identified as ICE during a federal operation in Davis; Hale alleged the stop helped ICE agents "
                "leave. Mayor Helen Marsh and Vice Mayor Gloria Ravel apologized and an administrative review "
                "is underway, while the Davis Police Department said it did not assist immigration enforcement."
            ),
            topic_tags=["immigration", "policing", "sanctuary-policy", "civil-rights"],
            involvements=[
                _inv("Helen Marsh", "PERSON", "Mayor co-issuing public apology", "APOLOGETIC",
                     "Apologized for the incident and reaffirmed the city's sanctuary commitments.",
                     ["Nothing matters more to us than the trust residents place in this city"]),
                _inv("Gloria Ravel", "PERSON", "Vice Mayor co-issuing public apology", "APOLOGETIC",
                     "Co-signed the apology and affirmed the city's obligation not to assist ICE.",
                     ["We want residents to know we take our duty not to assist ICE very seriously"]),
                _inv("Robin Hale", "PERSON", "Community observer cited by police", "OPPOSE",
                     "Followed a suspected ICE vehicle, was cited for horn use, and alleged the officer interfered.",
                     ["There is no question the officer wanted to stop me from following that truck"]),
                _inv("Valley Watch Network", "ORGANIZATION", "Group warning residents of ICE presence", "OPPOSE",
                     "Was attempting to warn community members of ICE presence during the incident."),
                _inv("ICE", "GOVERNMENT_BODY", "Federal agency operating in Davis", "PARTICIPANT",
                     "Operated in Davis using unmarked vehicles and allegedly alerted police it was being followed."),
                _inv("Officer Pemberton", "PERSON", "Officer who stopped and cited Hale", "PARTICIPANT",
                     "Pulled Hale over and issued a citation for improper horn use."),
                _inv("Davis Police Department", "GOVERNMENT_BODY", "Department whose officer made the stop", "DEFENSIVE",
                     "Said it had not assisted federal immigration enforcement and cited the stop as a horn violation.",
                     ["The department has not assisted with federal civil immigration enforcement and remains committed to state law"]),
                _inv("Owen Brackett", "PERSON", "City Manager overseeing administrative review", "INVESTIGATIVE",
                     "Said the city does not support ICE activities and urged residents to await the review.",
                     ["Let me be plain: the City of Davis does not cooperate with ICE enforcement, and staff have clear direction on that"]),
            ],
        ),
        ExtractedEvent(
            canonical_title="Sacramento City Manager Bars Police From Acting as Security for ICE Operations",
            event_type="POLICY_ANNOUNCEMENT",
            event_date_iso=None,
            summary=(
                "After three people associated with Valley Watch Network were detained by Sacramento police while monitoring "
                "immigration enforcement in late October, City Manager Lena Okafor issued a directive "
                "prohibiting officers from acting as security for ICE operations."
            ),
            topic_tags=["immigration", "policing"],
            involvements=[
                _inv("Lena Okafor", "PERSON", "Sacramento City Manager issuing directive", "SUPPORT",
                     "Issued a directive barring police from acting as security for ICE operations."),
                _inv("Valley Watch Network", "ORGANIZATION", "Members detained while monitoring ICE", "PARTICIPANT",
                     "Three associated individuals were detained while monitoring enforcement activity."),
            ],
        ),
    ],
)

MOCK_RESULT_2 = ArticleExtractionResult(
    affiliations=[
        ExtractedAffiliation(person_name="Owen Brackett", organization_name="City of Davis", role_title="City Manager"),
    ],
    events=[
        ExtractedEvent(
            canonical_title="Davis Police Officer Cites ICE Observer Robin Hale; Administrative Review Continues",
            event_type="PUBLIC_CONTROVERSY",
            event_date_iso="2026-11-05",
            summary=(
                "The City of Davis continues an administrative review after a Davis police officer cited Robin Hale "
                "for improper horn use while she followed a vehicle she identified as ICE. Mayor Helen Marsh and "
                "Vice Mayor Gloria Ravel apologized, and the Davis Police Department said it did not assist "
                "immigration enforcement."
            ),
            topic_tags=["immigration", "policing"],
            involvements=[
                _inv("Owen Brackett", "PERSON", "City Manager overseeing review", "INVESTIGATIVE",
                     "Asked residents to let the administrative review run its course."),
                _inv("Robin Hale", "PERSON", "Observer cited by police", "OPPOSE",
                     "Alleged the stop allowed ICE agents to drive away."),
                _inv("Davis Police Department", "GOVERNMENT_BODY", "Department under review", "DEFENSIVE",
                     "Maintained it did not assist federal immigration enforcement."),
            ],
        )
    ],
)
