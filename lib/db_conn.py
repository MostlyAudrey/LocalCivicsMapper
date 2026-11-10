"""Database connection for the website (sync psycopg 3 pool, dict rows).

Config is read from the `.db` file next to index.py (JSON), falling back to dev defaults:
    {"host": "...", "port": 5400, "dbname": "Davis", "user": "postgres", "password": "optional"}
Omit "password" to use ~/.pgpass. The pool is created lazily per process, so it is safe with gunicorn
pre-fork workers.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parents[1]
DEV_DEFAULTS = {"host": "localhost", "port": 5432, "dbname": "Davis", "user": "postgres"}

Params = Optional[Union[Sequence[Any], Dict[str, Any]]]


def load_db_config() -> Dict[str, Any]:
    path = Path(os.environ.get("CIVIC_DB_CONFIG", ROOT / ".db"))
    cfg = dict(DEV_DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text()))
    return cfg


class Db_conn:
    _pool: Optional[ConnectionPool] = None
    _pid: Optional[int] = None
    _lock = threading.Lock()

    def _get_pool(self) -> ConnectionPool:
        cls = type(self)
        if cls._pool is None or cls._pid != os.getpid():
            with cls._lock:
                if cls._pool is None or cls._pid != os.getpid():
                    cfg = load_db_config()
                    kwargs: Dict[str, Any] = {k: v for k, v in cfg.items()
                                              if k in ("host", "port", "dbname", "user", "password", "options")}
                    kwargs["row_factory"] = dict_row
                    cls._pool = ConnectionPool(kwargs=kwargs, min_size=1, max_size=int(cfg.get("pool_max", 8)), open=True)
                    cls._pid = os.getpid()
        return cls._pool  # type: ignore[return-value]

    def fetch_all(self, sql: str, params: Params = None) -> List[Dict[str, Any]]:
        with self._get_pool().connection() as conn:
            return conn.execute(sql, params).fetchall()

    def fetch_one(self, sql: str, params: Params = None) -> Optional[Dict[str, Any]]:
        with self._get_pool().connection() as conn:
            return conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Params = None) -> int:
        """Run a statement in its own transaction; returns rowcount."""
        with self._get_pool().connection() as conn:
            return conn.execute(sql, params).rowcount

    def transaction(self):
        """`with db.transaction() as conn:` for multi-statement atomic work (conn.execute(...))."""
        return self._get_pool().connection()
