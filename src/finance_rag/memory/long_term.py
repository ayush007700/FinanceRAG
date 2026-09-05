"""Long-term semantic memory, namespaced per user or organisation.

The checkpointer remembers a *conversation*; this remembers *facts* across
conversations -- a client's fiscal year end, which service lines an org engages,
a correction someone made last month. Recall is by embedding similarity within a
namespace, so it uses the same pgvector machinery as the corpus.

Namespacing is the tenancy boundary: a query never reaches another namespace's
rows, so one org cannot recall another's facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from finance_rag.db import connection, to_vector_literal
from finance_rag.embeddings import EmbeddingService
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class Memory:
    key: str
    content: str
    kind: str = "fact"
    score: float | None = None
    metadata: dict[str, Any] | None = None


class LongTermMemory:
    def __init__(self, embedder: EmbeddingService | None = None) -> None:
        self._embedder = embedder

    @property
    def embedder(self) -> EmbeddingService:
        if self._embedder is None:
            self._embedder = EmbeddingService()
        return self._embedder

    def remember(
        self,
        namespace: str,
        key: str,
        content: str,
        kind: str = "fact",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Upsert one memory. Re-remembering the same key replaces it."""
        embedding = self.embedder.embed_query(content)
        with connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent_memories
                        (namespace, key, content, kind, metadata, embedding)
                    VALUES
                        (:ns, :key, :content, :kind, CAST(:metadata AS jsonb),
                         CAST(:embedding AS vector))
                    ON CONFLICT (namespace, key) DO UPDATE SET
                        content   = EXCLUDED.content,
                        kind      = EXCLUDED.kind,
                        metadata  = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding,
                        updated_at = now()
                    """
                ),
                {
                    "ns": namespace,
                    "key": key,
                    "content": content,
                    "kind": kind,
                    "metadata": json.dumps(metadata or {}, default=str),
                    "embedding": to_vector_literal(embedding),
                },
            )
        logger.info("memory_stored", namespace=namespace, key=key, kind=kind)

    def recall(
        self, namespace: str, query: str, top_k: int = 5, min_similarity: float = 0.3
    ) -> list[Memory]:
        """Retrieve namespace-scoped memories similar to the query.

        ``min_similarity`` keeps loosely-related memories out of the prompt:
        an irrelevant "fact" injected into context is a hallucination source,
        not helpful background.
        """
        embedding = self.embedder.embed_query(query)
        with connection() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT key, content, kind, metadata,
                           1 - (embedding <=> CAST(:embedding AS vector)) AS score
                    FROM agent_memories
                    WHERE namespace = :ns AND embedding IS NOT NULL
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT :top_k
                    """
                ),
                {
                    "ns": namespace,
                    "embedding": to_vector_literal(embedding),
                    "top_k": top_k,
                },
            ).mappings()
            return [
                Memory(
                    key=r["key"],
                    content=r["content"],
                    kind=r["kind"],
                    score=float(r["score"]),
                    metadata=dict(r["metadata"] or {}),
                )
                for r in rows
                if float(r["score"]) >= min_similarity
            ]

    def forget(self, namespace: str, key: str) -> None:
        with connection() as conn:
            conn.execute(
                text("DELETE FROM agent_memories WHERE namespace = :ns AND key = :key"),
                {"ns": namespace, "key": key},
            )
