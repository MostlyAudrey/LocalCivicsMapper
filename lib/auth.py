"""Two-role authentication (flask_login), per CLAUDE.md:

* viewer - site-wide password (hash in TB_Web_Settings, changeable in /admin/settings)
* admin  - admin password (hash in .web); an admin session also satisfies @login_required

Changing the site password bumps `site_password_version`, which logs out every viewer session.
"""
from __future__ import annotations

import secrets
from functools import wraps

from flask import abort, current_app, redirect, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user
from werkzeug.security import check_password_hash, generate_password_hash

from lib import db_settings

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = None


class User(UserMixin):
    def __init__(self, role: str, version: str = "") -> None:
        self.role = role
        self.version = version

    def get_id(self) -> str:
        return f"{self.role}:{self.version}"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@login_manager.user_loader
def load_user(user_id: str):
    role, _, version = user_id.partition(":")
    if role == "admin":
        return User("admin")
    if role == "viewer" and version == db_settings.site_password_version():
        return User("viewer", version)
    return None  # site password changed -> session invalid


def check_site_password(password: str) -> bool:
    h = db_settings.get_setting("site_password_hash")
    return bool(h) and check_password_hash(h, password)


def check_admin_password(password: str) -> bool:
    h = current_app.config["WEB"].get("admin_password_hash")
    return bool(h) and check_password_hash(h, password)


def set_site_password(new_password: str) -> None:
    db_settings.set_setting("site_password_hash", generate_password_hash(new_password))
    db_settings.set_setting("site_password_version", secrets.token_hex(8))


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            if request.path.startswith("/admin/api/") or request.is_json:
                abort(403)
            return redirect(url_for("auth.admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------- CSRF
def csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = session["_csrf"] = secrets.token_urlsafe(32)
    return tok


def verify_csrf() -> None:
    sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf")
    if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
        abort(400, "CSRF token missing or invalid")
