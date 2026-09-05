"""Hybrid retrieval over Postgres/pgvector using Reciprocal Rank Fusion.

Three rankers contribute:

  1. dense  -- cosine similarity over pgvector embeddings
  2. sparse -- Postgres full-text ``ts_rank_cd``
  3. graph  -- chunks co-mentioning the same entities as the top candidates

Rankers 1 and 2 are fused inside a single SQL statement (see
``PgVectorStore.hybrid_search_rrf``). Ranker 3 runs as a second hop and is folded
in here with a configurable weight.

Why RRF instead of a weighted score blend:

    RRF(d) = sum over rankers r of  w_r / (k + rank_r(d))

Only rank *position* matters, so rankers on wildly different scales (unbounded
``ts_rank_cd`` vs. cosine in [-1, 1]) combine without normalisation or a tuned
alpha. The constant k (default 60) flattens the head of the curve, which makes
agreement across rankers outweigh being first in any single one.

The scores RRF produces are ordinal artefacts -- top-1 is always ~1/(k+1)
regardless of whether the corpus had anything relevant. Absolute judgements
(abstention in particular) therefore read ``RetrievedChunk.cosine``, which the
store returns untouched.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from finance_rag.config import get_settings
from finance_rag.embeddings import EmbeddingService
from finance_rag.models import Chunk, RetrievedChunk
from finance_rag.ranking.reranker import Reranker
from finance_rag.retrieval.fusion import rrf_contribution
from finance_rag.store import PgVectorStore


def _as_float(value: Any, default: float = 0.0) -> float:
    """Postgres numeric arithmetic arrives as Decimal; normalise to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_to_chunk(row: dict) -> Chunk:
    meta = row.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    return Chunk(
        chunk_id=row["chunk_id"],
        doc_id=row.get("doc_id") or meta.get("doc_id", ""),
        text=row.get("text") or "",
        index=_as_int(row.get("chunk_index")) or 0,
        tokens=_as_int(row.get("tokens")) or 0,
        section=row.get("section"),
        parent_id=row.get("parent_id"),
        metadata={
            **meta,
            "title": row.get("title") or meta.get("title"),
            "source": row.get("source") or meta.get("source"),
            "service_line": row.get("service_line") or meta.get("service_line"),
            "jurisdiction": row.get("jurisdiction") or meta.get("jurisdiction"),
            "modality": row.get("modality") or meta.get("modality") or "text",
        },
    )


class HybridRetriever:
    def __init__(
        self,
        store: PgVectorStore | None = None,
        embedder: EmbeddingService | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.settings = get_settings()
        self.store = store or PgVectorStore()
        self.embedder = embedder or EmbeddingService()
        self.reranker = reranker or Reranker()

    def retrieve(
        self,
        query: str,
        service_line: str | None = None,
        org_id: str | None = None,
        as_of: date | None = None,
    ) -> list[RetrievedChunk]:
        top_k = self.settings.retrieval_top_k

        query_vec = self.embedder.embed_query(query)
        rows = self.store.hybrid_search_rrf(
            embedding=query_vec,
            query_text=query,
            top_k=top_k,
            service_line=service_line,
            org_id=org_id,
            as_of=as_of,
        )

        by_id: dict[str, RetrievedChunk] = {}
        for row in rows:
            chunk = _row_to_chunk(row)
            rrf = _as_float(row.get("rrf_score"))
            by_id[chunk.chunk_id] = RetrievedChunk(
                chunk=chunk,
                score=rrf,
                rrf_score=rrf,
                cosine=_as_float(row.get("cosine")),
                vector_score=_as_float(row.get("dense_rrf")),
                bm25_score=_as_float(row.get("sparse_rrf")),
                dense_rank=_as_int(row.get("dense_rank")),
                sparse_rank=_as_int(row.get("sparse_rank")),
            )

        self._apply_graph_ranker(by_id)

        fused = sorted(by_id.values(), key=lambda x: x.score, reverse=True)[:top_k]
        self._attach_parent_context(fused)

        return self.reranker.rerank(query, fused, top_k=self.settings.rerank_top_k)

    def _apply_graph_ranker(self, by_id: dict[str, RetrievedChunk]) -> None:
        """Fold entity co-mention neighbours in as a weighted third ranker.

        Expansion results are ranked by how many entities they share with the
        seed set, then contribute ``w / (k + rank)`` on the same scale as the
        dense and sparse rankers -- keeping the fusion additive and comparable
        instead of bolting an arbitrary bonus onto an unrelated scale.
        """
        seeds = [r.chunk.chunk_id for r in sorted(
            by_id.values(), key=lambda x: x.score, reverse=True
        )[:8]]
        if not seeds:
            return

        graph_rows = self.store.graph_expand(
            seeds, limit=self.settings.graph_expand_limit
        )
        k = self.settings.rrf_k
        weight = self.settings.graph_expand_weight

        for rank, row in enumerate(graph_rows, start=1):
            contribution = rrf_contribution(rank, k=k, weight=weight)
            chunk = _row_to_chunk(row)
            existing = by_id.get(chunk.chunk_id)
            if existing is not None:
                existing.graph_score = contribution
                existing.graph_rank = rank
                existing.score += contribution
                existing.rrf_score += contribution
            else:
                # Neighbour that neither lexical nor dense search surfaced. It has
                # no cosine of its own here, so it can contribute context but must
                # never satisfy the absolute relevance floor on its own.
                by_id[chunk.chunk_id] = RetrievedChunk(
                    chunk=chunk,
                    score=contribution,
                    rrf_score=contribution,
                    graph_score=contribution,
                    graph_rank=rank,
                )

    def _attach_parent_context(self, fused: list[RetrievedChunk]) -> None:
        parent_ids = list({r.chunk.parent_id for r in fused if r.chunk.parent_id})
        if not parent_ids:
            return
        parents = {
            p["chunk_id"]: p for p in self.store.fetch_parent_context(parent_ids)
        }
        for item in fused:
            pid = item.chunk.parent_id
            if pid and pid in parents:
                item.chunk.metadata["parent_excerpt"] = (parents[pid]["text"] or "")[:1200]
