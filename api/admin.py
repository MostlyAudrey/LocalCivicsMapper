"""Admin backend (SPEC_WEB_UI §5.4 and §7), adapted for the separate worker process.

The web app never runs pipeline work: buttons enqueue TB_Worker_Command rows or flip TB_Scheduler_State,
and the dashboard polls /admin/api/status (worker heartbeat + progress written by the worker).
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash

from api.wiki import _json_safe
from lib import db_admin, db_metrics, db_settings
from lib.auth import admin_required, set_site_password

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------- dashboard
@admin_bp.route("")
@admin_required
def dashboard():
    return render_template("admin/dashboard.html", sources=db_admin.list_sources(),
                           site_password_set=db_settings.site_password_is_set())


@admin_bp.route("/api/status")
@admin_required
def api_status():
    w = db_admin.worker_status()
    alive = bool(w and w.get("heartbeat_age") is not None and w["heartbeat_age"] < db_admin.WORKER_STALE_SECONDS
                 and w.get("processor_state") != "STOPPED")
    return jsonify(_json_safe({
        "worker": w,
        "worker_alive": alive,
        "scheduler": db_admin.scheduler_state(),
        "counters": db_admin.counters(),
        "queue_by_source": db_admin.queue_by_source(),
        "runs": db_admin.recent_job_runs(15),
        "alerts": db_admin.open_alerts(),
        "commands": db_admin.recent_commands(8),
    }))


@admin_bp.route("/api/jobrun/<int:run_id>")
@admin_required
def api_jobrun(run_id: int):
    run = db_admin.job_run(run_id)
    if not run:
        abort(404)
    return jsonify(_json_safe(run))


@admin_bp.route("/api/command", methods=["POST"])
@admin_required
def api_command():
    body = request.get_json(silent=True) or {}
    params = body.get("params") or {}
    if body.get("command") == "BURST":  # paid API: validate bounds server-side, not just in the form
        try:
            params = {"limit": int(params.get("limit", 50)),
                      "max_spend_usd": float(params.get("max_spend_usd", 5)),
                      "concurrency": int(params.get("concurrency", 8))}
        except (TypeError, ValueError):
            return jsonify({"error": "limit, max_spend_usd and concurrency must be numbers"}), 400
        if not (1 <= params["limit"] <= 5000 and 0 < params["max_spend_usd"] <= 500
                and 1 <= params["concurrency"] <= 32):
            return jsonify({"error": "limit 1-5000, spend cap up to $500, concurrency 1-32"}), 400
    try:
        cid = db_admin.enqueue_command(body.get("command", ""), params)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "command_id": cid})


@admin_bp.route("/api/scheduler", methods=["POST"])
@admin_required
def api_scheduler():
    body = request.get_json(silent=True) or {}
    interval = body.get("interval_hours")
    if interval is not None:
        interval = float(interval)
        if not 0.25 <= interval <= 168:
            return jsonify({"error": "interval must be between 0.25 and 168 hours"}), 400
    state = db_admin.update_scheduler(
        is_enabled=body.get("is_enabled"), interval_hours=interval, processor_enabled=body.get("processor_enabled"))
    return jsonify(_json_safe({"ok": True, "scheduler": state}))


@admin_bp.route("/api/alerts/ack", methods=["POST"])
@admin_required
def api_ack_alerts():
    return jsonify({"ok": True, "acknowledged": db_admin.acknowledge_alerts()})


@admin_bp.route("/api/entities")
@admin_required
def api_entities():
    return jsonify(_json_safe(db_admin.search_entities(request.args.get("q", ""))))


# ---------------------------------------------------------------- performance metrics
METRIC_RANGES = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "all": 24 * 3650}


@admin_bp.route("/metrics")
@admin_required
def metrics_page():
    return render_template("admin/metrics.html", ranges=list(METRIC_RANGES))


@admin_bp.route("/api/metrics")
@admin_required
def api_metrics():
    rng = request.args.get("range", "7d")
    local_only = request.args.get("local_only", "1") != "0"  # default: hide API burst data
    return jsonify(_json_safe(db_metrics.metrics(METRIC_RANGES.get(rng, METRIC_RANGES["7d"]), local_only)))


# ---------------------------------------------------------------- sources & backfill
@admin_bp.route("/sources", methods=["GET", "POST"])
@admin_required
def sources():
    if request.method == "POST":
        f = request.form
        try:
            cfg = db_admin.parse_config_json(f.get("scrape_config", ""))
            sid = db_admin.save_source(
                int(f["id"]) if f.get("id") else None,
                name=f["name"].strip(), base_url=f["base_url"].strip(),
                sitemap_url=f.get("sitemap_url", "").strip() or None, rss_url=f.get("rss_url", "").strip() or None,
                is_active=f.get("is_active") == "on", scrape_config=cfg,
                reliability=float(f.get("reliability") or 1.0),
            )
            flash(f"Saved source #{sid}.", "ok")
        except (ValueError, KeyError) as exc:
            flash(f"Could not save source: {exc}", "error")
        return redirect(url_for("admin.sources"))
    return render_template("admin/sources.html", sources=db_admin.list_sources())


@admin_bp.route("/sources/<int:source_id>/toggle", methods=["POST"])
@admin_required
def toggle_source(source_id: int):
    db_admin.toggle_source(source_id)
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/backfill", methods=["POST"])
@admin_required
def backfill(source_id: int):
    f = request.form
    params = {"source_id": source_id,
              "backfill_days": int(f["backfill_days"]) if f.get("backfill_days") else None,
              "since": f.get("since") or None,
              "limit": int(f["limit"]) if f.get("limit") else None}
    if not params["backfill_days"] and not params["since"]:
        flash("Give either a number of days or a since-date.", "error")
    else:
        cid = db_admin.enqueue_command("BACKFILL", params)
        flash(f"Backfill queued (command #{cid}). The crawler only queues articles; the processor picks them up.", "ok")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/sources/<int:source_id>/crawl", methods=["POST"])
@admin_required
def crawl_source(source_id: int):
    cid = db_admin.enqueue_command("CRAWL_NOW", {"source_id": source_id})
    flash(f"Crawl queued (command #{cid}).", "ok")
    return redirect(url_for("admin.sources"))


# ---------------------------------------------------------------- failures
@admin_bp.route("/failures", methods=["GET", "POST"])
@admin_required
def failures():
    if request.method == "POST":
        action = request.form.get("action")
        ids = [int(x) for x in request.form.getlist("ids")]
        if action == "retry_all":
            flash(f"Re-queued {db_admin.retry_articles(None)} article(s).", "ok")
        elif action == "retry" and ids:
            flash(f"Re-queued {db_admin.retry_articles(ids)} article(s).", "ok")
        elif action == "skip" and ids:
            flash(f"Marked {db_admin.skip_articles(ids)} article(s) skipped.", "ok")
        elif action == "process_anyway" and ids:
            flash(f"Re-queued {db_admin.process_anyway(ids)} skipped article(s); triage is bypassed for them.", "ok")
        return redirect(url_for("admin.failures"))
    return render_template("admin/failures.html", articles=db_admin.failed_articles(),
                           skipped=db_admin.triage_skipped())


# ---------------------------------------------------------------- entities
@admin_bp.route("/entities/merge", methods=["GET", "POST"])
@admin_required
def merge():
    if request.method == "POST":
        try:
            res = db_admin.merge_entities(int(request.form["source_id"]), int(request.form["target_id"]))
            flash(f"Merged “{res['source_name']}” into “{res['target_name']}” "
                  f"({res['moved_event_rows']} event rows moved). Bio will be regenerated.", "ok")
            return redirect(url_for("wiki.entity", entity_id=res["target_id"]))
        except (ValueError, KeyError) as exc:
            flash(f"Merge failed: {exc}", "error")
        return redirect(url_for("admin.merge"))
    return render_template("admin/merge.html", candidates=db_admin.possible_duplicates(),
                           dismiss_ready=db_admin.not_duplicate_table_ready())


@admin_bp.route("/api/entities/<int:entity_id>/aliases", methods=["POST"])
@admin_required
def api_entity_aliases(entity_id: int):
    """Edit an entity's aliases: body {"add": "name"} or {"remove": "name"}."""
    body = request.get_json(silent=True) or {}
    try:
        if body.get("remove"):
            row = db_admin.remove_entity_alias(entity_id, str(body["remove"]))
        elif body.get("add"):
            row = db_admin.add_entity_alias(entity_id, str(body["add"]))
        else:
            return jsonify({"error": "give add or remove"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "aliases": row["aliases"]}) if row else (jsonify({"error": "not found"}), 404)


@admin_bp.route("/api/entities/not-duplicate", methods=["POST"])
@admin_required
def api_not_duplicate():
    """Dismiss a suggested pair: body {"a_id": .., "b_id": ..}. Never suggested again (needs migration v1.7)."""
    body = request.get_json(silent=True) or {}
    try:
        a_id, b_id = int(body["a_id"]), int(body["b_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "a_id and b_id are required"}), 400
    if not db_admin.not_duplicate_table_ready():
        return jsonify({"error": "apply migration sql/unapplied/v1.7_EntityNotDuplicate.sql first"}), 409
    try:
        db_admin.dismiss_duplicate(a_id, b_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@admin_bp.route("/entities/<int:entity_id>/regenerate-bio", methods=["POST"])
@admin_required
def regenerate_bio(entity_id: int):
    cid = db_admin.enqueue_command("REGENERATE_BIO", {"entity_id": entity_id})
    flash(f"Bio regeneration queued (command #{cid}); refresh in a minute or two.", "ok")
    return redirect(url_for("wiki.entity", entity_id=entity_id))


@admin_bp.route("/api/entity-event/<int:entity_event_id>/dispute", methods=["POST"])
@admin_required
def api_dispute(entity_event_id: int):
    """Hide one stance record (e.g. an op-ed misrepresenting someone). body: {"note": "..."}"""
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()
    if not note:
        return jsonify({"error": "please give a short reason"}), 400
    row = db_admin.set_dispute(entity_event_id, True, note)
    return (jsonify({"ok": True, "entity_event": _json_safe(row)}) if row else (jsonify({"error": "not found"}), 404))


@admin_bp.route("/api/entity-event/<int:entity_event_id>/restore", methods=["POST"])
@admin_required
def api_restore(entity_event_id: int):
    row = db_admin.set_dispute(entity_event_id, False)
    return (jsonify({"ok": True, "entity_event": _json_safe(row)}) if row else (jsonify({"error": "not found"}), 404))


@admin_bp.route("/api/entity/<int:entity_id>/disputed")
@admin_required
def api_disputed(entity_id: int):
    from lib import db_wiki

    return jsonify(_json_safe(db_wiki.entity_disputed(entity_id)))


# ---------------------------------------------------------------- settings
@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_page():
    from flask import current_app

    if request.method == "POST":
        pw, pw2 = request.form.get("new_password", ""), request.form.get("confirm_password", "")
        admin_hash = current_app.config["WEB"].get("admin_password_hash")
        if len(pw) < 8:
            flash("Use at least 8 characters.", "error")
        elif pw != pw2:
            flash("Passwords don't match.", "error")
        elif admin_hash and check_password_hash(admin_hash, pw):
            flash("The site password must be different from the admin password.", "error")
        else:
            set_site_password(pw)
            flash("Site password changed. Everyone signed in with the old password has been signed out.", "ok")
        return redirect(url_for("admin.settings_page"))
    return render_template("admin/settings.html", site_password_set=db_settings.site_password_is_set())
