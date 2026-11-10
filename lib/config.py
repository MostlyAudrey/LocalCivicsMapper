"""Website config from the `.web` file (JSON) next to index.py, per CLAUDE.md rule 4.

    {
      "secret_key": "<random>",                 # Flask session signing key
      "admin_password_hash": "<werkzeug hash>",  # set with: flask --app index set-admin-password
      "embed_base_url": "http://host:8002/v1",   # Qwen3-Embedding-4B (semantic search)
      "embed_model": "Qwen/Qwen3-Embedding-4B",
      "embed_dim": 1024
    }
The site-wide (viewer) password is NOT here: it lives hashed in TB_Web_Settings and is changed in /admin/settings.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
WEB_CONFIG_PATH = Path(os.environ.get("CIVIC_WEB_CONFIG", ROOT / ".web"))

DEFAULTS: Dict[str, Any] = {
    "secret_key": None,
    "admin_password_hash": None,
    "embed_base_url": "http://localhost:8002/v1",
    "embed_api_key": "local",
    "embed_model": "Qwen/Qwen3-Embedding-4B",
    "embed_dim": 1024,
    "session_days": 30,
}


def load_web_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    if WEB_CONFIG_PATH.exists():
        cfg.update(json.loads(WEB_CONFIG_PATH.read_text()))
    return cfg


def save_web_config(updates: Dict[str, Any]) -> None:
    cfg = {}
    if WEB_CONFIG_PATH.exists():
        cfg = json.loads(WEB_CONFIG_PATH.read_text())
    cfg.update(updates)
    WEB_CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(WEB_CONFIG_PATH, 0o600)
