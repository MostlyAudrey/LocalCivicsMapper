"""Website smoke test: auth gates, every page/endpoint, CSRF, password rules.

Run against a non-production schema, e.g. the preview schema:
    CIVIC_DB_CONFIG=/path/preview.db CIVIC_WEB_CONFIG=/path/preview.web .venv/bin/python tests/smoke_web.py
It sets test passwords in the given .web file and TB_Web_Settings - never point it at production config.
"""
import re
import sys

sys.path.insert(0, ".")
from werkzeug.security import generate_password_hash  # noqa: E402

from lib.config import WEB_CONFIG_PATH, save_web_config  # noqa: E402

assert WEB_CONFIG_PATH.name != ".web" or "--force" in sys.argv, "refusing to overwrite the real .web"
save_web_config({"admin_password_hash": generate_password_hash("admin-test-pw")})
from index import app  # noqa: E402
from lib.auth import set_site_password  # noqa: E402
from lib.db_conn import Db_conn  # noqa: E402

with app.app_context():
    set_site_password("viewer-test-pw")
app.config["TESTING"] = True


def csrf_of(html):
    m = re.search(r'name="csrf-token" content="([^"]+)"', html) or re.search(r'name="_csrf" value="([^"]+)"', html)
    return m.group(1)


def ok(client, url, label=""):
    r = client.get(url)
    print(f"{r.status_code} {label}{url} {len(r.data)}B")
    assert r.status_code == 200, r.get_data(as_text=True)[:3000]
    return r


c = app.test_client()
r = c.get("/"); assert r.status_code == 302 and "/login" in r.location
assert c.get("/api/suggest?q=da").status_code == 401
assert "/admin/login" in c.get("/admin").location
page = c.get("/login").get_data(as_text=True)
assert "Incorrect" in c.post("/login", data={"password": "wrong", "_csrf": csrf_of(page)}).get_data(as_text=True)
assert "Incorrect" in c.post("/login", data={"password": "admin-test-pw", "_csrf": csrf_of(page)}).get_data(as_text=True), \
    "admin password must not open the viewer login"
assert c.post("/login", data={"password": "viewer-test-pw", "_csrf": csrf_of(page)}).status_code == 302
for url in ["/", "/search?q=police", "/search?q=ice&mode=semantic", "/search?topic=immigration", "/search",
            "/api/suggest?q=davis", "/static/js/common.js"]:
    ok(c, url)
print("  suggest:", [s["label"] for s in c.get("/api/suggest?q=davis").get_json()][:5])
db = Db_conn()
eid = db.fetch_one("SELECT entity_id AS id FROM TB_Entity_Event GROUP BY entity_id ORDER BY COUNT(*) DESC LIMIT 1")["id"]
evid = db.fetch_one("SELECT id FROM TB_Event ORDER BY id LIMIT 1")["id"]
for url in [f"/wiki/entity/{eid}", f"/wiki/entity/{eid}?sort=recent", f"/api/entity/{eid}/ledger?sort=salience",
            f"/api/entity/{eid}/ledger?sort=recent", f"/api/entity/{eid}/ledger?sort=salience&topic=immigration",
            f"/wiki/event/{evid}"]:
    ok(c, url)
assert c.get("/wiki/entity/999999999").status_code == 404
led = c.get(f"/api/entity/{eid}/ledger?sort=salience").get_json()
print("  ledger rows:", len(led["rows"]), "| top:", led["rows"][0]["event_title"][:60], led["rows"][0]["salience_score"])
assert "/admin/login" in c.get("/admin").location, "viewer must not reach admin"
assert c.get("/admin/api/status").status_code in (401, 403)

a = app.test_client()
page = a.get("/admin/login").get_data(as_text=True)
assert "Incorrect" in a.post("/admin/login", data={"password": "viewer-test-pw", "_csrf": csrf_of(page)}).get_data(as_text=True)
assert a.post("/admin/login", data={"password": "admin-test-pw", "_csrf": csrf_of(page)}).status_code == 302
for url in ["/admin", "/admin/api/status", "/admin/sources", "/admin/failures", "/admin/entities/merge",
            "/admin/settings", "/admin/api/entities?q=dav", f"/wiki/entity/{eid}", "/"]:
    ok(a, url, "ADMIN ")
st = a.get("/admin/api/status").get_json()
print("  status:", sorted(st), "| articles:", st["counters"]["articles"], "| worker alive:", st["worker_alive"])
tok = csrf_of(a.get("/admin").get_data(as_text=True))
H = {"X-CSRF-Token": tok}
assert a.post("/admin/api/command", json={"command": "NOPE"}, headers=H).status_code == 400
assert a.post("/admin/api/command", json={"command": "CRAWL_NOW"}).status_code == 400, "missing CSRF must be rejected"
assert a.post("/admin/api/scheduler", json={"interval_hours": 0.01}, headers=H).status_code == 400
assert a.post("/admin/api/scheduler", json={"interval_hours": 6, "is_enabled": True}, headers=H).status_code == 200
r = a.post("/admin/settings", data={"_csrf": tok, "new_password": "admin-test-pw", "confirm_password": "admin-test-pw"},
           follow_redirects=True)
assert "must be different" in r.get_data(as_text=True)
r = a.post("/admin/settings", data={"_csrf": tok, "new_password": "new-viewer-pw", "confirm_password": "new-viewer-pw"},
           follow_redirects=True)
assert "Site password changed" in r.get_data(as_text=True)
assert c.get("/").status_code == 302, "old viewer sessions must be signed out after a password change"
print("ALL SMOKE CHECKS PASSED")
