"""Short-term conversational memory.

LangGraph's Postgres checkpointer persists AgentState per ``thread_id``, which
is what makes a follow-up like "and what about 2023?" resolvable: the graph
resumes from the previous turn's state instead of starting cold.

It shares the RDS instance the corpus already uses, so multi-turn memory adds no
infrastructure. The checkpointer owns its own tables and creates them via
``setup()``.
"""

from __future__ import annotations

from functools import lru_cache

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


def _psycopg_dsn(url: str) -> str:
    """SQLAlchemy URLs carry a driver suffix psycopg cannot parse."""
    return url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )


@lru_cache
def _pool():
    """Process-wide pool for the checkpointer.

    ``PostgresSaver.from_conn_string`` hands back a context manager; letting it
    fall out of scope closes the connection underneath the saver, which then
    fails on the next turn with "the connection is closed". An explicit pool
    owns the lifetime instead, and reconnects rather than dying on an RDS
    failover.

    autocommit and dict_row are required by the checkpointer's own queries.
    """
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    settings = get_settings()
    return ConnectionPool(
        conninfo=_psycopg_dsn(settings.database_url),
        max_size=settings.db_pool_size,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )


def build_checkpointer(setup: bool = True):
    """Return a Postgres checkpointer, or None when unavailable.

    Returning None rather than raising is deliberate: losing multi-turn memory
    degrades the product, while failing to start removes it entirely.
    """
    settings = get_settings()
    if not settings.conversation_memory_enabled:
        return None
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        # AgentState carries our own dataclasses. They must be declared, or
        # LangGraph deserialises them with a warning today and refuses outright
        # in a later version -- which would silently break multi-turn memory.
        serde = JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("finance_rag.models", "Chunk"),
                ("finance_rag.models", "RetrievedChunk"),
                ("finance_rag.models", "Citation"),
                ("finance_rag.models", "RetrievalMetrics"),
            ]
        )
        saver = PostgresSaver(_pool(), serde=serde)
        if setup:
            saver.setup()
        logger.info("checkpointer_ready", backend="postgres")
        return saver
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpointer_unavailable", error=str(exc))
        return None
