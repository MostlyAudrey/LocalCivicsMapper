"""Civic Wiki pages (SPEC_WEB_UI §6) + JSON endpoints used by static/js/wiki.js."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from lib import db_measures, db_wiki
from lib.embed import embed_query
from lib.wiki_links import render_wiki_links

wiki_bp = Blueprint("wiki", __name__)


def _json_safe(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


@wiki_bp.route("/")
@login_required
def home():
    return render_template("home.html", events=db_wiki.recent_events(15), top=db_wiki.most_active_entities(18),
                           counts=db_wiki.corpus_counts())


@wiki_bp.route("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    mode = request.args.get("mode", "lexical")
    topic = request.args.get("topic") or None
    results, error = {"entities": [], "events": [], "positions": []}, None
    if q:
        if mode == "semantic":
            try:
                results = db_wiki.semantic_search(embed_query(q), topic)
            except Exception as exc:  # embedding server down -> say so, don't 500
                error = f"Semantic search is unavailable right now ({type(exc).__name__}). Showing keyword results."
                results = db_wiki.lexical_search(q, topic)
                mode = "lexical"
        else:
            results = db_wiki.lexical_search(q, topic)
    elif topic:  # browse a topic: its events, no entity matching
        results = db_wiki.lexical_search("", topic)
        results["entities"] = []
    return render_template("search.html", q=q, mode=mode, topic=topic, results=results, error=error,
                           topics=db_wiki.all_topics())


@wiki_bp.route("/wiki/entity/<int:entity_id>")
@login_required
def entity(entity_id: int):
    ent = db_wiki.get_entity(entity_id)
    votes_ready = db_measures.tables_ready()
    if not ent:
        abort(404)
    return render_template(
        "entity.html", e=ent,
        affiliations=db_wiki.entity_affiliations_condensed(entity_id),
        members=db_wiki.entity_members(entity_id) if ent["entity_type"] != "PERSON" else None,
        topics=db_wiki.entity_topics(entity_id),
        allies=db_wiki.entity_coalition(entity_id, allies=True),
        opponents=db_wiki.entity_coalition(entity_id, allies=False),
        sort=request.args.get("sort", "salience"), topic=request.args.get("topic") or "",
        votes=db_measures.entity_votes(entity_id, request.args.get("vote_topic", "")) if votes_ready else [],
        vote_summary=db_measures.entity_vote_summary(entity_id) if votes_ready else [],
        vote_topic=request.args.get("vote_topic", ""),
    )


@wiki_bp.route("/wiki/event/<int:event_id>")
@login_required
def event(event_id: int):
    ev = db_wiki.get_event(event_id)
    if not ev:
        abort(404)
    actors = db_wiki.event_actors(event_id)
    sides = [s for s in db_wiki.event_sides(event_id) if s["entity_ids"]]
    if len(sides) >= 2:  # contested: group by side (an endorser of two candidates shows under both)
        placed = {i for s in sides for i in s["entity_ids"]}
        groups = [(s["label"], [a for a in actors if a["entity_id"] in s["entity_ids"]]) for s in sides]
        groups.append(("On no side", [a for a in actors if a["entity_id"] not in placed]))
        by = "side"
    else:
        order = ["SUPPORT", "OPPOSE", "APOLOGETIC", "DEFENSIVE", "INVESTIGATIVE", "PARTICIPANT", "NEUTRAL"]
        groups = [(s, [a for a in actors if a["stance"] == s]) for s in order]
        by = "stance"
    return render_template("event.html", ev=ev, articles=db_wiki.event_articles(event_id),
                           groups=[g for g in groups if g[1]], group_by=by)


@wiki_bp.route("/wiki/measures")
@login_required
def measures():
    if not db_measures.tables_ready():
        abort(404)
    q, j, t = request.args.get("q", ""), request.args.get("jurisdiction", ""), request.args.get("type", "")
    return render_template("measures.html", rows=db_measures.list_measures(q, j, t), q=q, jurisdiction=j, mtype=t)


@wiki_bp.route("/wiki/measure/<int:measure_id>")
@login_required
def measure(measure_id: int):
    if not db_measures.tables_ready():
        abort(404)
    m = db_measures.get_measure(measure_id)
    if not m:
        abort(404)
    explicit = db_measures.measure_events(measure_id)
    issue = db_measures.measure_issue_events(measure_id)
    people = db_measures.measure_people(measure_id, [e["id"] for e in explicit] + [e["id"] for e in issue])
    return render_template("measure.html", m=m, motions=db_measures.measure_motions(measure_id),
                           procedural_hidden=db_measures.count_procedural_motions(measure_id),
                           explicit=explicit, issue=issue,
                           supporters=[p for p in people if p["n_support"] > p["n_oppose"]],
                           opponents=[p for p in people if p["n_oppose"] > p["n_support"]],
                           others=[p for p in people if p["n_oppose"] == p["n_support"]])


# ---------------------------------------------------------------- JSON for static/js
@wiki_bp.route("/api/suggest")
@login_required
def api_suggest():
    return jsonify(_json_safe(db_wiki.suggest(request.args.get("q", ""))))


@wiki_bp.route("/api/entity/<int:entity_id>/ledger")
@login_required
def api_ledger(entity_id: int):
    sort = "recent" if request.args.get("sort") == "recent" else "salience"
    include_commentary = request.args.get("commentary") == "1"  # default: news only
    rows = db_wiki.entity_ledger(entity_id, sort, request.args.get("topic") or None,
                                 include_commentary=include_commentary)
    for r in rows:
        r["action_html"] = str(render_wiki_links(r.get("action_summary") or ""))
    is_admin = getattr(current_user, "is_admin", False)
    return jsonify({"sort": sort, "commentary": include_commentary, "is_admin": is_admin, "rows": _json_safe(rows)})
