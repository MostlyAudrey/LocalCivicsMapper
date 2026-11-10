"""Measures (ordinances, resolutions, contracts, budgets, projects...) and roll-call votes (migration v1.8).

Roll calls come only from official minutes. News coverage reaches a measure in two ways:
  * explicit: the article's event names the measure (TB_Measure_Event);
  * issue:    events about the same issue - close in meaning to the measure's title and sharing a topic tag,
              within a few years of the measure's last action.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from lib.db_conn import Db_conn

db = Db_conn()
Row = Dict[str, Any]

ISSUE_MIN_SIMILARITY = 0.55     # cosine, measure title vs event (EVENT_DEDUP embeddings)
ISSUE_YEARS = 3

# Procedural votes (approving minutes or the agenda, setting the meeting schedule, adjourning, recessing,
# tabling the remaining items) say nothing about a member's
# positions: hidden from voting records, measure pages and the measures list. A motion that also adopts a
# resolution/ordinance/contract/budget is NOT procedural (Postgres ARE regexes, case-insensitive).
PROCEDURAL = {
    "proc_words": (r"\m(minutes|agenda)\M"
                   r"|\mmeetings?\s+(schedule|time|times|dates?|calendar|location|day)\M"
                   r"|\mmeetings?\s+will\s+(be|begin|start|occur)\M"),
    "proc_substantive": r"\m(resolution|ordinance|contract|agreement|budget|appropriat\w*)\M",
    "proc_adjourn": r"^\W*(motion\s+to\s+)?(adjourn|recess|table\s+(all|the)\s+remaining)",
}


def _procedural(col: str) -> str:
    """SQL condition: column `col` (a trusted column name, never user input) reads as a procedural motion."""
    return (f"(({col} ~* %(proc_words)s AND {col} !~* %(proc_substantive)s) "
            f"OR {col} ~* %(proc_adjourn)s)")


def tables_ready() -> bool:
    row = db.fetch_one("SELECT to_regclass('tb_vote_motion') IS NOT NULL AS ok")
    return bool(row and row["ok"])


def get_measure(measure_id: int) -> Optional[Row]:
    return db.fetch_one(
        """SELECT m.*, b.canonical_name AS body_name,
                  (SELECT COUNT(*) FROM TB_Vote_Motion mo WHERE mo.measure_id = m.id) AS n_motions
           FROM TB_Measure m LEFT JOIN TB_Entity b ON b.id = m.body_entity_id WHERE m.id = %s""",
        (measure_id,),
    )


def count_procedural_motions(measure_id: int) -> int:
    row = db.fetch_one(
        f"SELECT COUNT(*) AS n FROM TB_Vote_Motion mo WHERE mo.measure_id = %(id)s AND {_procedural('mo.motion_text')}",
        {"id": measure_id, **PROCEDURAL},
    )
    return int(row["n"]) if row else 0


def measure_motions(measure_id: int) -> List[Row]:
    """Each substantive motion with its roll call (from the minutes) and the minutes document."""
    motions = db.fetch_all(
        f"""SELECT mo.*, a.url AS minutes_url, a.title AS minutes_title, b.canonical_name AS body_name
           FROM TB_Vote_Motion mo JOIN TB_Article a ON a.id = mo.source_article_id
           LEFT JOIN TB_Entity b ON b.id = mo.body_entity_id
           WHERE mo.measure_id = %(id)s AND NOT {_procedural('mo.motion_text')}
           ORDER BY mo.vote_date NULLS LAST, mo.id""",
        {"id": measure_id, **PROCEDURAL},
    )
    if not motions:
        return []
    votes = db.fetch_all(
        """SELECT v.motion_id, v.entity_id, v.raw_name, v.vote, e.canonical_name
           FROM TB_Vote v LEFT JOIN TB_Entity e ON e.id = v.entity_id
           WHERE v.motion_id = ANY(%s)
           ORDER BY array_position(ARRAY['AYE','NO','ABSTAIN','RECUSED','ABSENT'], v.vote), v.raw_name""",
        ([m["id"] for m in motions],),
    )
    by_motion: Dict[int, List[Row]] = {}
    for v in votes:
        by_motion.setdefault(v["motion_id"], []).append(v)
    for m in motions:
        m["votes"] = by_motion.get(m["id"], [])
    return motions


def measure_events(measure_id: int) -> List[Row]:
    """Events explicitly about the measure (named in an article, or a recorded vote), with their articles."""
    return db.fetch_all(
        """SELECT ev.id, ev.canonical_title, ev.event_type, ev.event_date, ev.summary, me.how,
                  COALESCE(json_agg(json_build_object('id', a.id, 'url', a.url, 'title', a.title, 'genre', a.genre,
                                                      'published_at', a.published_at, 'source', s.name)
                                    ORDER BY a.published_at) FILTER (WHERE a.id IS NOT NULL), '[]') AS articles
           FROM TB_Measure_Event me JOIN TB_Event ev ON ev.id = me.event_id
           LEFT JOIN TB_Article_Event ae ON ae.event_id = ev.id
           LEFT JOIN TB_Article a ON a.id = ae.article_id
           LEFT JOIN TB_Sources s ON s.id = a.source_id
           WHERE me.measure_id = %s
           GROUP BY ev.id, me.how ORDER BY ev.event_date NULLS LAST, ev.id""",
        (measure_id,),
    )


def measure_issue_events(measure_id: int, limit: int = 15) -> List[Row]:
    """Coverage of the wider issue: events close in meaning to the measure and sharing a topic tag, not already
    linked explicitly. Each with its articles."""
    return db.fetch_all(
        """WITH m AS (SELECT * FROM TB_Measure WHERE id = %(id)s),
           cand AS (
               SELECT ev.id, ev.canonical_title, ev.event_type, ev.event_date, ev.summary, ev.topic_tags,
                      1 - (ev.embedding <=> m.title_embedding) AS sim
               FROM TB_Event ev, m
               WHERE m.title_embedding IS NOT NULL AND ev.embedding IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM TB_Measure_Event x WHERE x.measure_id = m.id AND x.event_id = ev.id)
                 AND (m.last_action_date IS NULL OR ev.event_date IS NULL
                      OR ABS(ev.event_date - m.last_action_date) <= 365 * %(years)s)
               ORDER BY ev.embedding <=> m.title_embedding
               LIMIT 80)
           SELECT c.id, c.canonical_title, c.event_type, c.event_date, c.summary, ROUND(c.sim::numeric, 2) AS sim,
                  COALESCE(json_agg(json_build_object('id', a.id, 'url', a.url, 'title', a.title, 'genre', a.genre,
                                                      'published_at', a.published_at, 'source', s.name)
                                    ORDER BY a.published_at) FILTER (WHERE a.id IS NOT NULL), '[]') AS articles
           FROM cand c CROSS JOIN m
           LEFT JOIN TB_Article_Event ae ON ae.event_id = c.id
           LEFT JOIN TB_Article a ON a.id = ae.article_id
           LEFT JOIN TB_Sources s ON s.id = a.source_id
           WHERE c.sim >= %(min)s AND (cardinality(m.topic_tags) = 0 OR c.topic_tags && m.topic_tags)
           GROUP BY c.id, c.canonical_title, c.event_type, c.event_date, c.summary, c.sim
           ORDER BY c.sim DESC LIMIT %(limit)s""",
        {"id": measure_id, "years": ISSUE_YEARS, "min": ISSUE_MIN_SIMILARITY, "limit": limit},
    )


def measure_people(measure_id: int, event_ids: List[int], limit: int = 40) -> List[Row]:
    """People and organizations active on the measure / issue: stance counts across the given events
    (adequately weighted, undisputed rows; the acting body itself is left out)."""
    if not event_ids:
        return []
    return db.fetch_all(
        """SELECT e.id, e.canonical_name, e.entity_type,
                  COUNT(DISTINCT w.event_id) AS n_events,
                  COUNT(*) FILTER (WHERE w.stance IN ('SUPPORT', 'DEFENSIVE')) AS n_support,
                  COUNT(*) FILTER (WHERE w.stance IN ('OPPOSE', 'APOLOGETIC')) AS n_oppose,
                  BOOL_AND(w.genre = 'opinion') AS commentary_only,
                  EXISTS (SELECT 1 FROM TB_Vote v JOIN TB_Vote_Motion mo ON mo.id = v.motion_id
                          WHERE mo.measure_id = %(mid)s AND v.entity_id = e.id) AS voted
           FROM VW_Entity_Event_Weighted w JOIN TB_Entity e ON e.id = w.entity_id
           WHERE w.event_id = ANY(%(ev)s) AND NOT w.disputed AND w.weight >= 0.2
             AND e.id IS DISTINCT FROM (SELECT body_entity_id FROM TB_Measure WHERE id = %(mid)s)
           GROUP BY e.id ORDER BY n_events DESC, (COUNT(*)) DESC, e.canonical_name LIMIT %(limit)s""",
        {"mid": measure_id, "ev": event_ids, "limit": limit},
    )


def list_measures(q: str = "", jurisdiction: str = "", mtype: str = "", limit: int = 200) -> List[Row]:
    return db.fetch_all(
        f"""SELECT m.id, m.title, m.measure_type, m.measure_number, m.jurisdiction, m.status, m.last_action_date,
                  m.topic_tags, b.canonical_name AS body_name,
                  (SELECT COUNT(*) FROM TB_Vote_Motion mo WHERE mo.measure_id = m.id) AS n_motions,
                  (SELECT COUNT(*) FROM TB_Measure_Event me WHERE me.measure_id = m.id) AS n_events
           FROM TB_Measure m LEFT JOIN TB_Entity b ON b.id = m.body_entity_id
           WHERE (%(q)s = '' OR m.title ILIKE '%%' || %(q)s || '%%' OR m.measure_number ILIKE '%%' || %(q)s || '%%')
             AND (%(j)s = '' OR m.jurisdiction = %(j)s)
             AND (%(t)s = '' OR m.measure_type = %(t)s)
             -- procedural items ("Approval of March minutes") unless a substantive vote is filed under them
             AND NOT ({_procedural('m.title')}
                      AND NOT EXISTS (SELECT 1 FROM TB_Vote_Motion mo
                                      WHERE mo.measure_id = m.id AND NOT {_procedural('mo.motion_text')}))
           ORDER BY m.last_action_date DESC NULLS LAST, m.id DESC LIMIT %(limit)s""",
        {"q": q.strip(), "j": jurisdiction, "t": mtype, "limit": limit, **PROCEDURAL},
    )


def entity_votes(entity_id: int, topic: str = "", limit: int = 300) -> List[Row]:
    return db.fetch_all(
        f"""SELECT * FROM VW_Voting_Record
           WHERE entity_id = %(e)s AND (%(t)s = '' OR %(t)s = ANY(topic_tags))
             AND NOT {_procedural('motion_text')}
           ORDER BY vote_date DESC NULLS LAST, motion_id DESC LIMIT %(limit)s""",
        {"e": entity_id, "t": topic, "limit": limit, **PROCEDURAL},
    )


def entity_vote_summary(entity_id: int) -> List[Row]:
    """Per topic: how the member voted and how often against the majority."""
    return db.fetch_all(
        f"""SELECT t AS topic, COUNT(*) AS n,
                  COUNT(*) FILTER (WHERE vote = 'AYE') AS ayes, COUNT(*) FILTER (WHERE vote = 'NO') AS noes,
                  COUNT(*) FILTER (WHERE vote = 'ABSTAIN') AS abstentions,
                  COUNT(*) FILTER (WHERE vote IN ('ABSENT', 'RECUSED')) AS not_voting,
                  COUNT(*) FILTER (WHERE dissent) AS dissents
           FROM VW_Voting_Record, unnest(CASE WHEN cardinality(topic_tags) > 0 THEN topic_tags
                                               ELSE ARRAY['(untagged)'] END) t
           WHERE entity_id = %(e)s AND NOT {_procedural('motion_text')}
           GROUP BY t ORDER BY n DESC, t""",
        {"e": entity_id, **PROCEDURAL},
    )
