"""Login/logout for the two roles: site-wide viewer password and admin password."""
from __future__ import annotations

import time
from urllib.parse import urlparse

from flask import Blueprint, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user

from lib import db_settings
from lib.auth import User, check_admin_password, check_site_password, csrf_token, verify_csrf

auth_bp = Blueprint("auth", __name__)


def _safe_next(default: str) -> str:
    nxt = request.args.get("next") or request.form.get("next") or ""
    parsed = urlparse(nxt)
    if nxt.startswith("/") and not nxt.startswith("//") and not parsed.netloc:
        return nxt
    return default


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        verify_csrf()
        if check_site_password(request.form.get("password", "")):
            session.permanent = True
            login_user(User("viewer", db_settings.site_password_version()), remember=True)
            return redirect(_safe_next(url_for("wiki.home")))
        time.sleep(1.0)  # slow down guessing
        error = "Incorrect password."
    return render_template("login.html", error=error, admin=False, next=_safe_next(""),
                           site_password_set=db_settings.site_password_is_set(), csrf=csrf_token())


@auth_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        verify_csrf()
        if check_admin_password(request.form.get("password", "")):
            session.permanent = True
            login_user(User("admin"), remember=True)
            return redirect(_safe_next(url_for("admin.dashboard")))
        time.sleep(1.0)
        error = "Incorrect admin password."
    return render_template("login.html", error=error, admin=True, next=_safe_next(""),
                           site_password_set=True, csrf=csrf_token())


@auth_bp.route("/logout", methods=["POST"])
def logout():
    was_admin = getattr(current_user, "is_admin", False)
    logout_user()
    session.clear()
    return redirect(url_for("auth.admin_login" if was_admin else "auth.login"))
