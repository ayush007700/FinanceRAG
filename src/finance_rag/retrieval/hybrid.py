"""Hybrid retrieval: dense + BM25 + graph expansion + parent context."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict

from rank_bm25 import BM25Okapi

from finance_rag.config import get_settings
from finance_rag.embeddings import EmbeddingService
from finance_rag.graph import Neo4jKnowledgeStore
from finance_rag.models import Chunk, RetrievedChunk
from finance_rag.ranking.reranker import Reranker


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9§]+", text.lower())


def _minmax(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    vals = list(scores.values())
    lo, hi = min(vals), max(vals)
    if math.isclose(lo, hi):
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def _row_to_chunk(row: dict) -> Chunk:
    meta = {}
    if row.get("metadata_json"):
        try:
            meta = json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            meta = {}
    return Chunk(
        chunk_id=row["chunk_id"],
        doc_id=row.get("doc_id") or meta.get("doc_id", ""),
        text=row.get("text", ""),
        index=meta.get("index", 0),
        tokens=meta.get("tokens", 0),
        section=row.get("section"),
        parent_id=row.get("parent_id"),
        metadata={
            **meta,
            "title": row.get("title") or meta.get("title"),
            "source": row.get("source") or meta.get("source"),
            "service_line": row.get("service_line") or meta.get("service_line"),
            "jurisdiction": row.get("jurisdiction") or meta.get("jurisdiction"),
        },
    )


class HybridRetriever:
    def __init__(
        self,
        store: Neo4jKnowledgeStore | None = None,
        embedder: EmbeddingService | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.settings = get_settings()
        self.store = store or Neo4jKnowledgeStore()
        self.embedder = embedder or EmbeddingService()
        self.reranker = reranker or Reranker()
        self._bm25: BM25Okapi | None = None
        self._bm25_chunks: list[Chunk] = []

    def build_bm25_index(self, chunks: list[Chunk]) -> None:
        child_chunks = [c for c in chunks if c.metadata.get("level") == "child"]
        self._bm25_chunks = child_chunks
        corpus = [_tokenize(c.text) for c in child_chunks]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def _local_bm25(self, query: str, top_k: int) -> list[RetrievedChunk]:
        if not self._bm25 or not self._bm25_chunks:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        results = []
        for idx, score in ranked:
            results.append(
                RetrievedChunk(
                    chunk=self._bm25_chunks[idx],
                    score=float(score),
                    bm25_score=float(score),
                )
            )
        return results

    def retrieve(self, query: str, service_line: str | None = None) -> list[RetrievedChunk]:
        top_k = self.settings.retrieval_top_k
        alpha = self.settings.hybrid_alpha

        query_vec = self.embedder.embed_query(query)
        dense_rows = self.store.vector_search(query_vec, top_k=top_k, service_line=service_line)
        sparse_rows = self.store.fulltext_search(query, top_k=top_k)
        local_bm25 = self._local_bm25(query, top_k=top_k)

        dense_scores = {r["chunk_id"]: float(r["score"]) for r in dense_rows}
        sparse_scores = {r["chunk_id"]: float(r["score"]) for r in sparse_rows}
        for item in local_bm25:
            sparse_scores[item.chunk.chunk_id] = max(
                sparse_scores.get(item.chunk.chunk_id, 0.0), item.bm25_score
            )

        dense_n = _minmax(dense_scores)
        sparse_n = _minmax(sparse_scores)

        by_id: dict[str, RetrievedChunk] = {}
        for row in dense_rows + sparse_rows:
            chunk = _row_to_chunk(row)
            cid = chunk.chunk_id
            hybrid = alpha * dense_n.get(cid, 0.0) + (1 - alpha) * sparse_n.get(cid, 0.0)
            existing = by_id.get(cid)
            if not existing or hybrid > existing.score:
                by_id[cid] = RetrievedChunk(
                    chunk=chunk,
                    score=hybrid,
                    vector_score=dense_n.get(cid, 0.0),
                    bm25_score=sparse_n.get(cid, 0.0),
                )

        for item in local_bm25:
            cid = item.chunk.chunk_id
            if cid not in by_id:
                hybrid = (1 - alpha) * sparse_n.get(cid, 0.0)
                by_id[cid] = RetrievedChunk(
                    chunk=item.chunk,
                    score=hybrid,
                    bm25_score=sparse_n.get(cid, 0.0),
                )

        # Graph expansion boost
        seed_ids = list(by_id.keys())[:8]
        graph_rows = self.store.graph_expand(seed_ids, limit=8)
        for row in graph_rows:
            chunk = _row_to_chunk(row)
            boost = 0.05 * float(row.get("score", 1))
            if chunk.chunk_id in by_id:
                by_id[chunk.chunk_id].graph_score = boost
                by_id[chunk.chunk_id].score += boost
            else:
                by_id[chunk.chunk_id] = RetrievedChunk(
                    chunk=chunk, score=boost, graph_score=boost
                )

        fused = sorted(by_id.values(), key=lambda x: x.score, reverse=True)[:top_k]

        # Hierarchical: attach parent section context metadata
        parent_ids = [r.chunk.parent_id for r in fused if r.chunk.parent_id]
        parents = {
            p["chunk_id"]: p for p in self.store.fetch_parent_context(list(set(parent_ids)))
        }
        for item in fused:
            pid = item.chunk.parent_id
            if pid and pid in parents:
                item.chunk.metadata["parent_excerpt"] = parents[pid]["text"][:1200]

        reranked = self.reranker.rerank(query, fused, top_k=self.settings.rerank_top_k)
        return reranked
