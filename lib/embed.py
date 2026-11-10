"""Query embeddings for semantic search (sync). Mirrors civic_tracker.pipeline.encoder for the
SEARCH_QUERY task: Qwen3 instruction prefix, MRL slice to embed_dim, L2-normalise."""
from __future__ import annotations

import math
from typing import List

import httpx
from flask import current_app

SEARCH_QUERY_INSTRUCTION = (
    "Given a policy issue, stance, or civic topic query, retrieve relevant public official "
    "actions, decisions, and verbatim statements."
)


def embed_query(text: str) -> List[float]:
    cfg = current_app.config["WEB"]
    q = " ".join(text.strip().split())
    payload = {"model": cfg["embed_model"], "input": [f"Instruct: {SEARCH_QUERY_INSTRUCTION}\nQuery: {q}"],
               "encoding_format": "float"}
    r = httpx.post(cfg["embed_base_url"].rstrip("/") + "/embeddings", json=payload, timeout=30,
                   headers={"Authorization": f"Bearer {cfg.get('embed_api_key', 'local')}"})
    r.raise_for_status()
    vec = r.json()["data"][0]["embedding"][: int(cfg["embed_dim"])]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def to_vector_literal(vec: List[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
