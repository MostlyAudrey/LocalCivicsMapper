"""Client for the local Qwen/Qwen3-Embedding-4B OpenAI-compatible server.

Qwen3-Embedding-4B natively outputs 2560 dims and expects `Instruct: ...\\nQuery: ...`
prefixes for queries and symmetric (dedup / clustering) tasks; asymmetric target documents
are encoded raw. We MRL-slice to `settings.EMBED_DIM` and L2-renormalise so pgvector HNSW
cosine indexes work at full speed.
"""
from __future__ import annotations

import math
from typing import List, Literal, Protocol

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from civic_tracker.config import settings

TaskType = Literal[
    "ENTITY_DEDUP",     # Symmetric: matching TB_Entity profiles across articles
    "EVENT_DEDUP",      # Symmetric: matching TB_Event records across articles
    "ARTICLE_CLUSTER",  # Symmetric: clustering paragraphs or detecting duplicate wire stories
    "SEARCH_QUERY",     # Asymmetric query: CLI semantic search over positions/quotes
    "SEARCH_DOCUMENT",  # Asymmetric document: stored TB_Entity_Event.action_summary
]

TASK_INSTRUCTIONS = {
    "ENTITY_DEDUP": (
        "Given a civic entity profile with its name, type, role, and affiliated organization, "
        "retrieve or match equivalent profiles referring to the same real-world person or organization."
    ),
    "EVENT_DEDUP": (
        "Given a summary of a local news event or civic decision, match it with descriptions "
        "of the exact same underlying real-world incident, meeting, or controversy."
    ),
    "ARTICLE_CLUSTER": (
        "Identify the core civic topic, policy issue, and actors discussed in this news passage."
    ),
    "SEARCH_QUERY": (
        "Given a policy issue, stance, or civic topic query, retrieve relevant public official "
        "actions, decisions, and verbatim statements."
    ),
}


class Encoder(Protocol):
    async def embed(self, texts: List[str], task: TaskType) -> List[List[float]]: ...


def format_qwen3_input(text: str, task: TaskType) -> str:
    cleaned = " ".join(text.strip().split())
    if task == "SEARCH_DOCUMENT":
        # Asymmetric documents are encoded raw, without an instruction prefix.
        return cleaned
    return f"Instruct: {TASK_INSTRUCTIONS[task]}\nQuery: {cleaned}"


def truncate_and_l2_normalize(vec: List[float], dim: int) -> List[float]:
    sliced = vec[:dim]
    norm = math.sqrt(sum(x * x for x in sliced))
    if norm == 0.0:
        return sliced
    return [x / norm for x in sliced]


class LocalEncoderClient:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(base_url=settings.EMBED_BASE_URL, api_key=settings.EMBED_API_KEY)
        self.model = settings.EMBED_MODEL
        self.dim = settings.EMBED_DIM
        self.batch_size = settings.EMBED_BATCH_SIZE

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20), reraise=True)
    async def _embed_batch(self, batch: List[str]) -> List[List[float]]:
        resp = await self.client.embeddings.create(model=self.model, input=batch, encoding_format="float")
        ordered = sorted(resp.data, key=lambda item: item.index)
        return [truncate_and_l2_normalize(item.embedding, self.dim) for item in ordered]

    async def embed(self, texts: List[str], task: TaskType) -> List[List[float]]:
        if not texts:
            return []
        formatted = [format_qwen3_input(t, task) for t in texts]
        results: List[List[float]] = []
        for i in range(0, len(formatted), self.batch_size):
            results.extend(await self._embed_batch(formatted[i : i + self.batch_size]))
        return results
