"""Civic Wiki - Flask entry point.

    gunicorn -c gunicorn.conf.py index:app

Every page requires the site-wide password (viewer) except the login pages; /admin/* requires the admin
password. See CLAUDE.md for the overall design (the pipeline worker runs separately).
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta

import click
from flask import Flask, redirect, request, url_for
from flask_login import current_user
from werkzeug.security import generate_password_hash

from api.admin import admin_bp
from api.auth import auth_bp
from api.wiki import wiki_bp
from lib.auth import csrf_token, login_manager, verify_csrf
from lib.config import load_web_config, save_web_config
from lib.wiki_links import render_bio, render_wiki_links

STANCE_COLORS = {
    "SUPPORT": "bg-emerald-100 text-emerald-800 ring-emerald-300",
    "OPPOSE": "bg-rose-100 text-rose-800 ring-rose-300",
    "APOLOGETIC": "bg-amber-100 text-amber-800 ring-amber-300",
    "DEFENSIVE": "bg-orange-100 text-orange-800 ring-orange-300",
    "INVESTIGATIVE": "bg-purple-100 text-purple-800 ring-purple-300",
    "NEUTRAL": "bg-slate-100 text-slate-700 ring-slate-300",
    "PARTICIPANT": "bg-slate-100 text-slate-700 ring-slate-300",
}
TYPE_COLORS = {
    "PERSON": "bg-sky-100 text-sky-800",
    "GOVERNMENT_BODY": "bg-indigo-100 text-indigo-800",
    "ORGANIZATION": "bg-teal-100 text-teal-800",
    "COALITION": "bg-fuchsia-100 text-fuchsia-800",
}
PUBLIC_ENDPOINTS = {"auth.login", "auth.admin_login", "static"}


def create_app() -> Flask:
    app = Flask(__name__)
    web = load_web_config()
    if not web.get("secret_key"):
        web["secret_key"] = secrets.token_hex(32)
        save_web_config({"secret_key": web["secret_key"]})
    app.config["WEB"] = web
    app.config.update(
        SECRET_KEY=web["secret_key"],
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=int(web.get("session_days", 30))),
        REMEMBER_COOKIE_DURATION=timedelta(days=int(web.get("session_days", 30))),
        REMEMBER_COOKIE_HTTPONLY=True,
    )

    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(wiki_bp)
    app.register_blueprint(admin_bp)

    @app.before_request
    def site_gate():
        """Site-wide password: nothing but the login pages and static files without a session."""
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not current_user.is_authenticated:
            if request.path.startswith("/api/") or request.path.startswith("/admin/api/"):
                return {"error": "login required"}, 401
            if request.path.startswith("/admin"):
                return redirect(url_for("auth.admin_login", next=request.path))
            return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
        if request.method == "POST":
            verify_csrf()
        return None

    @app.context_processor
    def inject():
        return {
            "csrf_token": csrf_token,
            "is_admin": current_user.is_authenticated and getattr(current_user, "is_admin", False),
        }

    # globals (not context-processor vars) so imported macros in _macros.html can use them
    app.jinja_env.globals["stance_color"] = lambda s: STANCE_COLORS.get(s or "", STANCE_COLORS["NEUTRAL"])
    app.jinja_env.globals["type_color"] = lambda t: TYPE_COLORS.get(t or "", "bg-slate-100 text-slate-700")

    def fmt_date(v, fmt="%b %-d, %Y"):
        if not v:
            return "—"
        if isinstance(v, str):
            return v
        return v.strftime(fmt) if isinstance(v, (date, datetime)) else str(v)

    app.jinja_env.filters["wiki_links"] = render_wiki_links
    app.jinja_env.filters["bio"] = render_bio
    app.jinja_env.filters["date"] = fmt_date
    app.jinja_env.filters["type_label"] = lambda t: (t or "").replace("_", " ").title()

    @app.cli.command("set-admin-password")
    @click.password_option(help="New admin password (prompted if omitted).")
    def set_admin_password(password: str) -> None:
        """Store the admin password hash in .web (must differ from the site password)."""
        save_web_config({"admin_password_hash": generate_password_hash(password)})
        click.echo("Admin password updated in .web")

    @app.cli.command("set-site-password")
    @click.password_option(help="New site-wide viewer password.")
    def set_site_password_cmd(password: str) -> None:
        """Bootstrap the site-wide password from the command line (normally changed in /admin/settings)."""
        from werkzeug.security import check_password_hash

        from lib.auth import set_site_password

        admin_hash = load_web_config().get("admin_password_hash")
        if admin_hash and check_password_hash(admin_hash, password):
            raise click.ClickException("The site password must be different from the admin password.")
        set_site_password(password)
        click.echo("Site password updated (all viewer sessions signed out).")

    return app


app = create_app()
