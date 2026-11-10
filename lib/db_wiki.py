"""Read-side queries for the Civic Wiki pages (home, search, entity dossier, event page)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from lib import affiliations
from lib.db_conn import Db_conn
from lib.embed import to_vector_literal

db = Db_conn()
Row = Dict[str, Any]


# ---------------------------------------------------------------- home
def recent_events(limit: int = 15) -> List[Row]:
    events = db.fetch_all(
        """
        SELECT ev.id, ev.canonical_title, ev.event_type, ev.event_date, ev.topic_tags, ev.summary,
               (SELECT COUNT(*) FROM TB_Article_Event ae WHERE ae.event_id = ev.id) AS n_articles
        FROM TB_Event ev
        ORDER BY ev.event_date DESC NULLS LAST, ev.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    if not events:
        return events
    actors = db.fetch_all(
        """
        SELECT DISTINCT ON (ee.event_id, ee.entity_id) ee.event_id, e.id, e.canonical_name, e.entity_type, ee.stance
        FROM TB_Entity_Event ee JOIN TB_Entity e ON e.id = ee.entity_id
        WHERE ee.event_id = ANY(%s) AND NOT ee.disputed
        ORDER BY ee.event_id, ee.entity_id, ee.confidence DESC, ee.id DESC
        """,
        ([e["id"] for e in events],),
    )
    by_event: Dict[int, List[Row]] = {}
    for a in actors:
        by_event.setdefault(a["event_id"], []).append(a)
    for e in events:
        e["actors"] = sorted(by_event.get(e["id"], []), key=lambda a: (a["entity_type"] != "PERSON", a["canonical_name"]))
    return events


def most_active_entities(limit: int = 18) -> List[Row]:
    return db.fetch_all(
        """
        SELECT e.id, e.canonical_name, e.entity_type, COUNT(DISTINCT ee.event_id) AS n_events,
               MAX(ev.event_date) AS last_event
        FROM TB_Entity e
        JOIN TB_Entity_Event ee ON ee.entity_id = e.id
        JOIN TB_Event ev ON ev.id = ee.event_id
        GROUP BY e.id
        ORDER BY n_events DESC, last_event DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    )


def corpus_counts() -> Row:
    return db.fetch_one(
        """
        SELECT (SELECT COUNT(*) FROM TB_Entity) AS entities,
               (SELECT COUNT(*) FROM TB_Event) AS events,
               (SELECT COUNT(*) FROM TB_Article WHERE processing_status = 'PROCESSED') AS articles,
               (SELECT COUNT(*) FROM TB_Sources WHERE is_active) AS sources
        """
    ) or {}


# ---------------------------------------------------------------- search
def suggest(q: str, limit: int = 8) -> List[Row]:
    """Omnibox dropdown: entities (trgm on name + aliases) then events (title)."""
    q = q.strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    ents = db.fetch_all(
        """
        SELECT 'entity' AS kind, e.id, e.canonical_name AS label, e.entity_type AS sub,
               GREATEST(similarity(e.canonical_name, %(q)s),
                        COALESCE((SELECT MAX(similarity(a, %(q)s)) FROM unnest(e.aliases) a), 0)) AS score
        FROM TB_Entity e
        WHERE e.canonical_name ILIKE %(like)s OR e.canonical_name %% %(q)s
           OR EXISTS (SELECT 1 FROM unnest(e.aliases) a WHERE a ILIKE %(like)s)
        ORDER BY (e.canonical_name ILIKE %(prefix)s) DESC, score DESC
        LIMIT %(lim)s
        """,
        {"q": q, "like": like, "prefix": f"{q}%", "lim": limit},
    )
    evs = db.fetch_all(
        """
        SELECT 'event' AS kind, id, canonical_title AS label, to_char(event_date, 'YYYY-MM-DD') AS sub,
               similarity(canonical_title, %(q)s) AS score
        FROM TB_Event
        WHERE canonical_title ILIKE %(like)s OR canonical_title %% %(q)s
        ORDER BY score DESC
        LIMIT %(lim)s
        """,
        {"q": q, "like": like, "lim": max(3, limit // 2)},
    )
    return ents + evs


def lexical_search(q: str, topic: Optional[str] = None, limit: int = 30) -> Dict[str, List[Row]]:
    like = f"%{q}%"
    entities = db.fetch_all(
        """
        SELECT e.id, e.canonical_name, e.entity_type, e.aliases,
               (SELECT COUNT(DISTINCT event_id) FROM TB_Entity_Event WHERE entity_id = e.id) AS n_events,
               GREATEST(similarity(e.canonical_name, %(q)s),
                        COALESCE((SELECT MAX(similarity(a, %(q)s)) FROM unnest(e.aliases) a), 0)) AS score
        FROM TB_Entity e
        WHERE e.canonical_name ILIKE %(like)s OR e.canonical_name %% %(q)s
           OR EXISTS (SELECT 1 FROM unnest(e.aliases) a WHERE a ILIKE %(like)s OR a %% %(q)s)
        ORDER BY score DESC, n_events DESC
        LIMIT %(lim)s
        """,
        {"q": q, "like": like, "lim": limit},
    )
    events = db.fetch_all(
        """
        SELECT ev.id, ev.canonical_title, ev.event_type, ev.event_date, ev.topic_tags, ev.summary,
               GREATEST(similarity(ev.canonical_title, %(q)s), word_similarity(%(q)s, ev.summary)) AS score
        FROM TB_Event ev
        WHERE (ev.canonical_title ILIKE %(like)s OR ev.summary ILIKE %(like)s
               OR ev.canonical_title %% %(q)s OR %(q)s <%% ev.summary)
          AND (%(topic)s::TEXT IS NULL OR %(topic)s = ANY(ev.topic_tags))
        ORDER BY score DESC, ev.event_date DESC NULLS LAST
        LIMIT %(lim)s
        """,
        {"q": q, "like": like, "topic": topic, "lim": limit},
    )
    return {"entities": entities, "events": events, "positions": []}


def semantic_search(qvec: List[float], topic: Optional[str] = None, limit: int = 25) -> Dict[str, List[Row]]:
    v = to_vector_literal(qvec)
    # Nearest 3x candidates by meaning, then re-rank by similarity x evidence strength (v1.5 weights), so a
    # position known only from an op-ed or a third-party claim ranks below reported positions.
    positions = db.fetch_all(
        """
        SELECT ee.id, e.id AS entity_id, e.canonical_name, e.entity_type, ev.id AS event_id, ev.canonical_title,
               ev.event_date, ee.stance, ee.role_in_event, ee.action_summary, ee.direct_quotes,
               art.url AS source_url, w.source_name, w.genre, w.evidence_type, w.claimed_by, w.weight,
               1 - (ee.action_embedding <=> %(v)s::vector) AS similarity
        FROM TB_Entity_Event ee
        JOIN VW_Entity_Event_Weighted w ON w.id = ee.id
        JOIN TB_Entity e ON e.id = ee.entity_id
        JOIN TB_Event ev ON ev.id = ee.event_id
        JOIN TB_Article art ON art.id = ee.source_article_id
        WHERE ee.action_embedding IS NOT NULL AND NOT ee.disputed
          AND (%(topic)s::TEXT IS NULL OR %(topic)s = ANY(ev.topic_tags))
        ORDER BY ee.action_embedding <=> %(v)s::vector
        LIMIT %(lim)s
        """,
        {"v": v, "topic": topic, "lim": limit * 3},
    )
    for p in positions:
        p["score"] = float(p["similarity"]) * (0.4 + 0.6 * min(float(p["weight"] or 0), 1.0))
    positions = sorted(positions, key=lambda p: p["score"], reverse=True)[:limit]
    events = db.fetch_all(
        """
        SELECT ev.id, ev.canonical_title, ev.event_type, ev.event_date, ev.topic_tags, ev.summary,
               1 - (ev.embedding <=> %(v)s::vector) AS score
        FROM TB_Event ev
        WHERE ev.embedding IS NOT NULL AND (%(topic)s::TEXT IS NULL OR %(topic)s = ANY(ev.topic_tags))
        ORDER BY ev.embedding <=> %(v)s::vector
        LIMIT %(lim)s
        """,
        {"v": v, "topic": topic, "lim": 10},
    )
    return {"entities": [], "events": events, "positions": positions}


def all_topics(limit: int = 60) -> List[Row]:
    return db.fetch_all(
        """SELECT t AS tag, COUNT(*) AS n FROM TB_Event, unnest(topic_tags) t
           GROUP BY t ORDER BY n DESC, t LIMIT %s""",
        (limit,),
    )


# ---------------------------------------------------------------- entity dossier
def get_entity(entity_id: int) -> Optional[Row]:
    return db.fetch_one(
        """SELECT id, canonical_name, entity_type, aliases, summary_bio, bio_updated_at, bio_is_dirty,
                  (SELECT COUNT(DISTINCT event_id) FROM TB_Entity_Event WHERE entity_id = TB_Entity.id) AS n_events,
                  (SELECT COUNT(*) FROM TB_Entity_Affiliation WHERE entity_id = TB_Entity.id) AS n_affs
           FROM TB_Entity WHERE id = %s""",
        (entity_id,),
    )


def entity_affiliations(entity_id: int) -> List[Row]:
    return db.fetch_all(
        """
        SELECT a.role_title, a.organization_id, o.canonical_name AS organization,
               a.first_seen_date, a.last_seen_date
        FROM TB_Entity_Affiliation a LEFT JOIN TB_Entity o ON o.id = a.organization_id
        WHERE a.entity_id = %s
        ORDER BY a.last_seen_date DESC, a.first_seen_date DESC
        """,
        (entity_id,),
    )


def entity_affiliations_condensed(entity_id: int) -> List[Row]:
    """Affiliations grouped by organization, synonymous roles merged (lib/affiliations.py)."""
    return affiliations.condense(entity_affiliations(entity_id))


def entity_members(entity_id: int, limit: int = 25) -> List[Row]:
    """People/orgs affiliated WITH this organization (reverse affiliations), one line per person."""
    rows = db.fetch_all(
        """
        SELECT e.id, e.canonical_name, a.role_title, a.last_seen_date, o.canonical_name AS organization
        FROM TB_Entity_Affiliation a JOIN TB_Entity e ON e.id = a.entity_id
        JOIN TB_Entity o ON o.id = a.organization_id
        WHERE a.organization_id = %s
        ORDER BY a.last_seen_date DESC LIMIT 500
        """,
        (entity_id,),
    )
    return affiliations.condense_members(rows)[:limit]


def entity_topics(entity_id: int) -> List[Row]:
    return db.fetch_all(
        """
        SELECT t AS tag, COUNT(DISTINCT ev.id) AS n
        FROM TB_Entity_Event ee JOIN TB_Event ev ON ev.id = ee.event_id, unnest(ev.topic_tags) t
        WHERE ee.entity_id = %s
        GROUP BY t ORDER BY n DESC, t
        """,
        (entity_id,),
    )


# Coalition rules (mirrored in civic_tracker stage3_wiki for the bio context; keep the two in sync):
# - an "ally" needs this many shared events: one shared event (often one article) is not a coalition;
# - an ally/opponent pair needs at least one *reputable* article that puts both entities in the same
#   event: source reliability >= REPUTABLE_MIN_RELIABILITY, not an opinion piece, neither row "claimed"
#   (asserted by a third party) or disputed.
# - the other entity must be a recurring player: at least MIN_COUNTERPART_EVENTS events of its own overall (one
#   court case or one crime report doesn't make a defendant an "opponent" of the DA) AND show up beyond one
#   case file: a role/affiliation, an event that isn't a court/police action (community activity), or coverage
#   from 2+ sources. (One case often yields several events - charges, indictment, sentencing - from one source.)
MIN_ALLY_SHARED_EVENTS = 2
REPUTABLE_MIN_RELIABILITY = 0.7
MIN_COUNTERPART_EVENTS = 2

_REPUTABLE_LINK_SQL = """
  AND (SELECT count(DISTINCT z.event_id) FROM TB_Entity_Event z
       WHERE z.entity_id = m.entity_b_id AND NOT z.disputed) >= %(min_counterpart)s
  AND (EXISTS (SELECT 1 FROM TB_Entity_Affiliation af WHERE af.entity_id = m.entity_b_id)
       OR EXISTS (SELECT 1 FROM TB_Entity_Event z JOIN TB_Event ze ON ze.id = z.event_id
                  WHERE z.entity_id = m.entity_b_id AND NOT z.disputed
                    AND ze.event_type NOT IN ('LEGAL_ACTION', 'LAW_ENFORCEMENT_ACTION'))
       OR (SELECT count(DISTINCT za.source_id) FROM TB_Entity_Event z
           JOIN TB_Article za ON za.id = z.source_article_id
           WHERE z.entity_id = m.entity_b_id AND NOT z.disputed) >= 2)
  AND EXISTS (
      SELECT 1
      FROM TB_Entity_Event x
      JOIN TB_Entity_Event y ON y.event_id = x.event_id AND y.source_article_id = x.source_article_id
      JOIN TB_Article a ON a.id = x.source_article_id
      JOIN TB_Sources s ON s.id = a.source_id
      WHERE x.entity_id = m.entity_a_id AND y.entity_id = m.entity_b_id
        AND NOT x.disputed AND NOT y.disputed
        AND s.reliability >= %(min_rel)s
        AND COALESCE(a.genre, 'unknown') <> 'opinion'
        AND COALESCE(x.evidence_type, 'reported') <> 'claimed'
        AND COALESCE(y.evidence_type, 'reported') <> 'claimed')
"""


def entity_coalition(entity_id: int, allies: bool, limit: int = 8) -> List[Row]:
    params = {"eid": entity_id, "min_rel": REPUTABLE_MIN_RELIABILITY, "min_shared": MIN_ALLY_SHARED_EVENTS,
              "min_counterpart": MIN_COUNTERPART_EVENTS, "limit": limit}
    if allies:
        return db.fetch_all(
            """SELECT m.* FROM VW_Entity_Coalition_Matrix m
               WHERE m.entity_a_id = %(eid)s AND m.alignment_index > 0
                 AND m.shared_events_count >= %(min_shared)s""" + _REPUTABLE_LINK_SQL + """
               ORDER BY m.shared_events_count DESC, m.alignment_index DESC LIMIT %(limit)s""",
            params,
        )
    return db.fetch_all(
        """SELECT m.* FROM VW_Entity_Coalition_Matrix m
           WHERE m.entity_a_id = %(eid)s AND m.alignment_index < 0""" + _REPUTABLE_LINK_SQL + """
           ORDER BY m.opposing_stance_count DESC, m.alignment_index ASC LIMIT %(limit)s""",
        params,
    )


def _sources_for(article_ids: List[int]) -> Dict[int, Row]:
    if not article_ids:
        return {}
    rows = db.fetch_all(
        """SELECT a.id, a.url, a.title, a.published_at, s.name AS source_name
           FROM TB_Article a JOIN TB_Sources s ON s.id = a.source_id WHERE a.id = ANY(%s)""",
        (list(set(article_ids)),),
    )
    return {r["id"]: r for r in rows}


MIN_EVIDENCE_WEIGHT = 0.2  # same threshold the coalition matrix uses (v1.5)


def _entity_records(entity_id: int) -> Dict[int, List[Row]]:
    """Every non-disputed stance record for the entity with provenance + weight, grouped by event."""
    rows = db.fetch_all(
        """
        SELECT id AS entity_event_id, event_id, source_article_id, source_name, source_url AS url,
               article_title AS title, genre, evidence_type, claimed_by, weight
        FROM VW_Entity_Event_Weighted
        WHERE entity_id = %s AND NOT disputed
        ORDER BY weight DESC, published_at DESC
        """,
        (entity_id,),
    )
    out: Dict[int, List[Row]] = {}
    for r in rows:
        r["commentary"] = r["genre"] == "opinion" or r["evidence_type"] == "claimed"
        out.setdefault(r["event_id"], []).append(r)
    return out


def entity_ledger(entity_id: int, sort: str = "salience", topic: Optional[str] = None, limit: int = 200,
                  include_commentary: bool = False) -> List[Row]:
    """Career & stance ledger. sort=salience -> VW_Entity_Salient_Career_Events (one row per event, rolled up,
    salience scaled by evidence weight); sort=recent -> VW_Entity_Career_Ledger (one row per article mention).
    Default ("news only") hides items supported only by weak evidence (opinion pieces, third-party claims,
    low-reliability sources: weight < 0.2). Disputed records never appear. Each row carries its `records`
    (source, genre, evidence type, weight, entity_event_id) for provenance badges and the admin dispute action."""
    if sort == "salience":
        rows = db.fetch_all(
            """
            SELECT event_id, event_title, event_type, event_date, topic_tags, entity_article_citations,
                   total_event_articles, primary_role_in_event AS role_in_event, primary_stance AS stance,
                   combined_action_summary AS action_summary, all_direct_quotes AS direct_quotes,
                   salience_score, evidence_weight, has_commentary, commentary_only, provisional
            FROM VW_Entity_Salient_Career_Events
            WHERE entity_id = %(eid)s AND (%(topic)s::TEXT IS NULL OR %(topic)s = ANY(topic_tags))
              AND (%(all)s OR NOT commentary_only)
            ORDER BY salience_score DESC, event_date DESC NULLS LAST
            LIMIT %(lim)s
            """,
            {"eid": entity_id, "topic": topic, "lim": limit, "all": include_commentary},
        )
        records = _entity_records(entity_id)
        for r in rows:
            r["records"] = [x for x in records.get(r["event_id"], [])
                            if include_commentary or x["weight"] >= MIN_EVIDENCE_WEIGHT] or records.get(r["event_id"], [])
    else:
        rows = db.fetch_all(
            """
            SELECT l.event_id, l.event_title, l.event_type, l.effective_date AS event_date, l.topic_tags,
                   l.role_in_event, l.stance, l.action_summary, l.direct_quotes, l.entity_event_id, l.weight,
                   l.provisional, s.salience_score, s.entity_article_citations, s.total_event_articles
            FROM VW_Entity_Career_Ledger l
            LEFT JOIN VW_Entity_Salient_Career_Events s ON s.entity_id = l.entity_id AND s.event_id = l.event_id
            WHERE l.entity_id = %(eid)s AND (%(topic)s::TEXT IS NULL OR %(topic)s = ANY(l.topic_tags))
              AND (%(all)s OR l.weight >= %(min)s)
            ORDER BY l.effective_date DESC NULLS LAST, l.event_id DESC
            LIMIT %(lim)s
            """,
            {"eid": entity_id, "topic": topic, "lim": limit, "all": include_commentary, "min": MIN_EVIDENCE_WEIGHT},
        )
        by_id = {x["entity_event_id"]: x for recs in _entity_records(entity_id).values() for x in recs}
        for r in rows:
            r["records"] = [by_id[r["entity_event_id"]]] if r["entity_event_id"] in by_id else []
    return rows


def entity_disputed(entity_id: int) -> List[Row]:
    """Disputed stance records (admin view, for undo)."""
    return db.fetch_all(
        """
        SELECT ee.id AS entity_event_id, ev.canonical_title AS event_title, ee.stance, ee.action_summary,
               ee.dispute_note, ee.disputed_at, s.name AS source_name, a.url
        FROM TB_Entity_Event ee
        JOIN TB_Event ev ON ev.id = ee.event_id
        JOIN TB_Article a ON a.id = ee.source_article_id
        JOIN TB_Sources s ON s.id = a.source_id
        WHERE ee.entity_id = %s AND ee.disputed
        ORDER BY ee.disputed_at DESC
        """,
        (entity_id,),
    )


# ---------------------------------------------------------------- event page
def get_event(event_id: int) -> Optional[Row]:
    return db.fetch_one("SELECT * FROM TB_Event WHERE id = %s", (event_id,))


def event_articles(event_id: int) -> List[Row]:
    return db.fetch_all(
        """
        SELECT a.id, a.url, a.title, a.author, a.published_at, a.genre, s.name AS source_name
        FROM TB_Article_Event ae JOIN TB_Article a ON a.id = ae.article_id JOIN TB_Sources s ON s.id = a.source_id
        WHERE ae.event_id = %s ORDER BY a.published_at
        """,
        (event_id,),
    )


def event_actors(event_id: int) -> List[Row]:
    """One card per actor: primary stance + all summaries/quotes across articles."""
    return db.fetch_all(
        """
        SELECT e.id AS entity_id, e.canonical_name, e.entity_type,
               (ARRAY_AGG(ee.stance ORDER BY
                    CASE WHEN ee.stance IN ('SUPPORT','OPPOSE','APOLOGETIC','DEFENSIVE','INVESTIGATIVE') THEN 1 ELSE 2 END,
                    ee.confidence DESC))[1] AS stance,
               (ARRAY_AGG(ee.role_in_event ORDER BY length(ee.role_in_event) DESC))[1] AS role_in_event,
               ARRAY_AGG(DISTINCT ee.action_summary) AS summaries,
               ARRAY_REMOVE(ARRAY_AGG(DISTINCT q), NULL) AS quotes
        FROM TB_Entity_Event ee
        JOIN TB_Entity e ON e.id = ee.entity_id
        LEFT JOIN LATERAL unnest(ee.direct_quotes) q ON TRUE
        WHERE ee.event_id = %s AND NOT ee.disputed
        GROUP BY e.id
        ORDER BY e.entity_type <> 'PERSON', e.canonical_name
        """,
        (event_id,),
    )
