"""Shared fixtures.

DB tests need TEST_DATABASE_URL (e.g. postgresql://postgres@host:5400/Davis_test). Each test
session creates a throwaway schema `ct_test_<random>`, applies schema.sql into it, and drops it
afterwards. The role needs CREATE on the database, and pgvector/pg_trgm must be installable
(or already installed).

`--run-live` swaps the offline fakes for real fastcoref/GLiNER, DeepSeek and Qwen3 endpoints.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from typing import Dict, List, Sequence

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from civic_tracker.config import settings
from civic_tracker.db.connection import load_schema_sql


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False,
                     help="Use real Stage 1 models, DeepSeek and Qwen3 endpoints.")


@pytest.fixture(scope="session")
def run_live(request) -> bool:
    return bool(request.config.getoption("--run-live"))


@pytest.fixture(autouse=True)
def _no_live_triage(monkeypatch):
    """Worker() builds a Triage that calls the real LLM; tests that want triage pass a fake explicitly."""
    from civic_tracker.config import settings

    monkeypatch.setattr(settings, "TRIAGE_ENABLED", False)


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def pool():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    schema = f"ct_test_{uuid.uuid4().hex[:8]}"

    async def configure(conn):
        conn.row_factory = dict_row

    p = AsyncConnectionPool(url, min_size=1, max_size=4, open=False, configure=configure,
                            kwargs={"options": f"-c search_path={schema},public"})
    await p.open(wait=True)
    async with p.connection() as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(load_schema_sql())
        await conn.execute(load_schema_sql("schema_web.sql"))
        await conn.execute(load_schema_sql("schema_web_v15.sql"))
        await conn.execute(load_schema_sql("schema_web_v16.sql"))
        await conn.execute(load_schema_sql("schema_web_v17.sql"))
        await conn.execute(load_schema_sql("schema_web_v18.sql"))
    try:
        yield p
    finally:
        async with p.connection() as conn:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await p.close()


# ---------------------------------------------------------------------------
# Offline fakes
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r"[a-z0-9]+")


class FakeEncoder:
    """Deterministic hashed bag-of-words (unigram + bigram) embeddings. Similar text => high cosine."""

    def __init__(self, dim: int = settings.EMBED_DIM) -> None:
        self.dim = dim

    def _vec(self, text: str) -> List[float]:
        toks = _TOKEN.findall(text.lower())
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        v = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def embed(self, texts: List[str], task) -> List[List[float]]:
        return [self._vec(t) for t in texts]


_FEMALE = {"Robin Hale": "Robin Hale", "Hale": "Robin Hale", "Helen Marsh": "Helen Marsh", "Gloria Ravel": "Gloria Ravel"}
_MALE = {"Owen Brackett": "Owen Brackett", "Brackett": "Owen Brackett", "Officer Pemberton": "Officer Pemberton", "Pemberton": "Officer Pemberton"}


def fake_coref(text: str):
    """Link she/he/her/his to the nearest preceding known name of matching gender."""
    clusters: Dict[str, list] = {}
    for m in re.finditer(r"\b([Ss]he|[Hh]e|[Hh]er|[Hh]is)\b", text):
        names = _FEMALE if m.group(1).lower() in ("she", "her") else _MALE
        best = None
        for n, head in names.items():
            i = text.rfind(n, 0, m.start())
            if i != -1 and (best is None or i > best[0]):
                best = (i, head)
        if best:
            head = best[1]
            hi = text.find(head)
            if hi == -1:
                continue
            clusters.setdefault(head, [(hi, hi + len(head))]).append((m.start(), m.end()))
    return list(clusters.values())


FAKE_NER_TERMS = {
    "Helen Marsh": "public official", "Gloria Ravel": "public official", "Robin Hale": "person",
    "Valley Watch Network": "activist organization", "ICE": "government agency", "Officer Pemberton": "law enforcement officer",
    "Davis Police Department": "government agency", "Owen Brackett": "law enforcement officer",
    "Lena Okafor": "public official",
}


def fake_ner(text: str, labels: Sequence[str]):
    out = []
    for term, label in FAKE_NER_TERMS.items():
        for m in re.finditer(rf"\b{re.escape(term)}\b", text):
            out.append({"start": m.start(), "end": m.end(), "text": term, "label": label, "score": 0.9})
    return out


def regex_sentences(text: str):
    return [(m.start(), m.end()) for m in re.finditer(r"[^.!?]+(?:[.!?]+[\"”]?|$)", text) if m.group().strip()]
