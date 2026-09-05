"""SQLAlchemy engine + connection pool for the Postgres/pgvector store."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


@lru_cache
def get_engine() -> Engine:
    """Process-wide pooled engine. Cached so every store shares one pool."""
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,  # survives RDS failover / idle disconnects
        echo=settings.db_echo,
        future=True,
    )
    logger.info("db_engine_created", pool_size=settings.db_pool_size)
    return engine


@contextmanager
def connection() -> Iterator[Connection]:
    """Autocommitting connection scope."""
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


def to_vector_literal(embedding: list[float]) -> str:
    """Render a Python list as a pgvector literal.

    Passing the vector as text plus an explicit ``::vector`` cast keeps the
    driver free of type-adapter registration and works identically on
    psycopg2/psycopg3.
    """
    return "[" + ",".join(f"{float(v):.8g}" for v in embedding) + "]"


def healthcheck() -> bool:
    try:
        with connection() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("db_healthcheck_failed", error=str(exc))
        return False
