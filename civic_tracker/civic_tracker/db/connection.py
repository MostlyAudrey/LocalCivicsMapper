"""psycopg async connection pool + schema initialisation."""
from __future__ import annotations

from importlib import resources
from typing import Optional, Sequence

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from civic_tracker.config import settings

_pool: Optional[AsyncConnectionPool] = None


def to_vector_literal(vec: Optional[Sequence[float]]) -> Optional[str]:
    """Render a Python float sequence as a pgvector text literal ('[0.1,0.2,...]').

    Queries cast it with `%s::vector`, so no adapter registration is needed.
    """
    if vec is None:
        return None
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"


async def _configure(conn: AsyncConnection) -> None:
    conn.row_factory = dict_row  # type: ignore[assignment]


async def get_pool(conninfo: Optional[str] = None, **conn_kwargs) -> AsyncConnectionPool:
    """Return the process-wide pool, opening it on first use."""
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo or settings.DATABASE_URL,
            min_size=settings.DB_POOL_MIN,
            max_size=settings.DB_POOL_MAX,
            configure=_configure,
            kwargs=conn_kwargs or None,
            open=False,
        )
        await _pool.open(wait=True)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def load_schema_sql(name: str = "schema.sql") -> str:
    return resources.files("civic_tracker.db").joinpath(name).read_text(encoding="utf-8")


async def init_schema(pool: AsyncConnectionPool, name: str = "schema.sql") -> None:
    """Execute schema.sql / schema_web.sql (idempotent: IF NOT EXISTS / OR REPLACE throughout)."""
    sql = load_schema_sql(name)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(sql)  # type: ignore[arg-type]
