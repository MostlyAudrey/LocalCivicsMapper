"""Test fixtures: a SYNTHETIC news scenario (a police stop of an ICE observer and the city's apology).

Every person, quote and organization except public bodies (City of Davis, Davis Police Department, ICE) is made
up; nothing here is copied from a real article. ARTICLE_2 is a follow-up written for the event-dedup test.
MOCK_RESULT_* are hand-written Stage 2 outputs for offline mode; their quotes are verbatim from ARTICLE_1_TEXT.
"""
from datetime import datetime, timezone

from civic_tracker.pipeline.stage2_llm import (
    ArticleExtractionResult,
    ExtractedAffiliation,
    ExtractedEvent,
    ExtractedInvolvement,
)

ARTICLE_1_URL = "https://news.example/2026/11/davis-mayor-apologizes-observer-stop/"
ARTICLE_1_TITLE = "Davis Mayor and Vice Mayor Apologize After Police Cite ICE Observer"
ARTICLE_1_PUBLISHED = datetime(2026, 11, 7, 12, 15, tzinfo=timezone.utc)
ARTICLE_1_TEXT = (
    "DAVIS, Calif. — Davis Mayor Helen Marsh and Vice Mayor Gloria Ravel issued a public apology Friday after a "
    "Davis police officer cited a resident who was following a vehicle she believed belonged to U.S. Immigration "
    "and Customs Enforcement (ICE).\n\n"
    "Robin Hale, a volunteer with the Valley Watch Network, said she spotted the unmarked vehicle near Cowell "
    "Boulevard on Wednesday morning and began following it to warn neighbors. She alleged that the vehicle "
    "stopped beside a Davis patrol car. “They talked for a minute while I waited,” Hale said. “The ICE truck "
    "pulled out and I followed it,” she added.\n\n"
    "Moments later, Officer Pemberton of the Davis Police Department pulled Hale over. “I pulled over right "
    "away. He didn’t explain anything. He kept me there and wrote a ticket for honking,” Hale said. “There is "
    "no question the officer wanted to stop me from following that truck.”\n\n"
    "In a joint statement, Marsh and Ravel apologized to the community. “Nothing matters more to us than the "
    "trust residents place in this city,” Marsh wrote. Ravel added: “We want residents to know we take our duty "
    "not to assist ICE very seriously.”\n\n"
    "The Davis Police Department said the citation was for improper horn use. “The department has not assisted "
    "with federal civil immigration enforcement and remains committed to state law,” the department said.\n\n"
    "City Manager Owen Brackett said an administrative review is underway. “Let me be plain: the City of Davis "
    "does not cooperate with ICE enforcement, and staff have clear direction on that,” Brackett said. He asked "
    "residents to let the review run its course.\n\n"
    "In Sacramento, City Manager Lena Okafor recently barred police from acting as security for ICE operations "
    "after three Valley Watch Network volunteers were detained while observing enforcement activity there.\n\n"
    "The City of Davis has not said when the review will be finished or whether its findings will be released."
)

ARTICLE_2_URL = "https://news.example/2026/11/test-followup-davis-observer-citation/"
ARTICLE_2_TITLE = "Davis Review of ICE Observer Citation Continues"
ARTICLE_2_PUBLISHED = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)  # 5 days later
ARTICLE_2_TEXT = (
    "DAVIS, Calif. — The City of Davis is continuing its administrative review of last week's traffic stop "
    "in which Officer Pemberton of the Davis Police Department cited Robin Hale, a community member monitoring "
    "ICE activity with the Valley Watch Network, for improper horn use near Cowell Boulevard. "
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


_DAVIS_TITLE = "Davis Police Officer Cites ICE Observer Robin Hale; Mayor and Vice Mayor Apologize"

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
                "identified as ICE; Hale alleged the stop helped ICE agents leave. Mayor Helen Marsh and Vice "
                "Mayor Gloria Ravel apologized and an administrative review is underway, while the Davis Police "
                "Department said it did not assist immigration enforcement."
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
                     "Its volunteer was trying to warn neighbors of ICE presence during the incident."),
                _inv("ICE", "GOVERNMENT_BODY", "Federal agency operating in Davis", "PARTICIPANT",
                     "Operated in Davis using an unmarked vehicle."),
                _inv("Officer Pemberton", "PERSON", "Officer who stopped and cited Hale", "PARTICIPANT",
                     "Pulled Hale over and issued a citation for improper horn use."),
                _inv("Davis Police Department", "GOVERNMENT_BODY", "Department whose officer made the stop", "DEFENSIVE",
                     "Said it had not assisted federal immigration enforcement and cited the stop as a horn violation.",
                     ["The department has not assisted with federal civil immigration enforcement and remains committed to state law"]),
                _inv("Owen Brackett", "PERSON", "City Manager overseeing administrative review", "INVESTIGATIVE",
                     "Said the city does not cooperate with ICE and urged residents to await the review.",
                     ["Let me be plain: the City of Davis does not cooperate with ICE enforcement, and staff have clear direction on that"]),
            ],
        ),
        ExtractedEvent(
            canonical_title="Sacramento City Manager Bars Police From Acting as Security for ICE Operations",
            event_type="POLICY_ANNOUNCEMENT",
            event_date_iso=None,
            summary=(
                "After three Valley Watch Network volunteers were detained while observing immigration enforcement "
                "in Sacramento, City Manager Lena Okafor barred police from acting as security for ICE operations."
            ),
            topic_tags=["immigration", "policing"],
            involvements=[
                _inv("Lena Okafor", "PERSON", "Sacramento City Manager issuing directive", "SUPPORT",
                     "Issued a directive barring police from acting as security for ICE operations."),
                _inv("Valley Watch Network", "ORGANIZATION", "Volunteers detained while observing ICE", "PARTICIPANT",
                     "Three volunteers were detained while observing enforcement activity."),
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
