"""Key/value website settings in TB_Web_Settings (site password hash + session version)."""
from __future__ import annotations

from typing import Optional

from lib.db_conn import Db_conn

db = Db_conn()


def get_setting(key: str) -> Optional[str]:
    row = db.fetch_one("SELECT value FROM TB_Web_Settings WHERE key = %s", (key,))
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO TB_Web_Settings (key, value, updated_at) VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (key, value),
    )


def site_password_version() -> str:
    return get_setting("site_password_version") or ""


def site_password_is_set() -> bool:
    return bool(get_setting("site_password_hash"))
