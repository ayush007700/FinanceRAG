"""Redis exact + semantic answer cache for FinanceRAG."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict

import numpy as np

from finance_rag.config import get_settings
from finance_rag.embeddings import EmbeddingService
from finance_rag.logging_setup import get_logger
from finance_rag.models import Citation, RAGResponse, RetrievalMetrics

logger = get_logger(__name__)

PREFIX = "finrag"


def _cosine(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _hash_key(text: str, service_line: str | None, corpus_version: str) -> str:
    raw = f"{corpus_version}|{service_line or ''}|{text.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class SemanticCache:
    def __init__(self, embedder: EmbeddingService | None = None) -> None:
        self.settings = get_settings()
        self.embedder = embedder or EmbeddingService()
        self._redis = None
        if self.settings.cache_enabled:
            try:
                import redis

                self._redis = redis.Redis.from_url(
                    self.settings.redis_url, decode_responses=True
                )
                self._redis.ping()
                logger.info("redis_connected", url=self.settings.redis_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("redis_unavailable", error=str(exc))
                self._redis = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.cache_enabled and self._redis is not None)

    def corpus_version(self) -> str:
        if not self._redis:
            return "0"
        return self._redis.get(f"{PREFIX}:corpus_version") or "0"

    def bump_corpus_version(self) -> str:
        if not self._redis:
            return "0"
        ver = str(self._redis.incr(f"{PREFIX}:corpus_version"))
        # Drop semantic index membership; payloads expire via TTL naturally.
        self._redis.delete(f"{PREFIX}:sem:ids")
        logger.info("corpus_version_bumped", version=ver)
        return ver

    def _serialize(self, response: RAGResponse, cache_layer: str) -> str:
        payload = {
            "answer": response.answer,
            "citations": [asdict(c) for c in response.citations],
            "confidence": response.confidence,
            "metrics": asdict(response.metrics),
            "guardrails": response.guardrails,
            "refused": response.refused,
            "trace_id": response.trace_id,
            "cache_hit": True,
            "cache_layer": cache_layer,
            "retrieved_ids": list(response.retrieved_ids),
            "cached_at": time.time(),
        }
        return json.dumps(payload)

    def _deserialize(self, raw: str, cache_layer: str) -> RAGResponse:
        data = json.loads(raw)
        citations = [Citation(**c) for c in data.get("citations") or []]
        metrics = RetrievalMetrics(**(data.get("metrics") or {}))
        return RAGResponse(
            answer=data.get("answer") or "",
            citations=citations,
            confidence=float(data.get("confidence") or 0.0),
            metrics=metrics,
            guardrails=list(data.get("guardrails") or []),
            refused=bool(data.get("refused")),
            trace_id=data.get("trace_id"),
            cache_hit=True,
            cache_layer=cache_layer,
            retrieved_ids=list(data.get("retrieved_ids") or []),
        )

    def get(
        self, query: str, service_line: str | None = None, query_embedding: list[float] | None = None
    ) -> RAGResponse | None:
        if not self.enabled:
            return None
        assert self._redis is not None
        version = self.corpus_version()
        exact_id = _hash_key(query, service_line, version)
        exact_key = f"{PREFIX}:exact:{exact_id}"
        cached = self._redis.get(exact_key)
        if cached:
            logger.info("cache_hit_exact", key=exact_id)
            return self._deserialize(cached, "exact")

        # Semantic: compare against recent entries
        emb = query_embedding or self.embedder.embed_query(query)
        ids = list(self._redis.smembers(f"{PREFIX}:sem:ids"))[: self.settings.cache_semantic_max_scan]
        best_id = None
        best_score = -1.0
        for entry_id in ids:
            vec_raw = self._redis.get(f"{PREFIX}:sem:{entry_id}:vec")
            if not vec_raw:
                continue
            vec = json.loads(vec_raw)
            score = _cosine(emb, vec)
            if score > best_score:
                best_score = score
                best_id = entry_id

        if best_id and best_score >= self.settings.cache_semantic_threshold:
            payload = self._redis.get(f"{PREFIX}:sem:{best_id}:payload")
            if payload:
                logger.info("cache_hit_semantic", key=best_id, score=best_score)
                return self._deserialize(payload, "semantic")
        return None

    def set(
        self,
        query: str,
        response: RAGResponse,
        service_line: str | None = None,
        query_embedding: list[float] | None = None,
    ) -> None:
        if not self.enabled or response.refused:
            return
        assert self._redis is not None
        version = self.corpus_version()
        exact_id = _hash_key(query, service_line, version)
        ttl = self.settings.cache_ttl_seconds
        payload = self._serialize(response, "exact")
        self._redis.setex(f"{PREFIX}:exact:{exact_id}", ttl, payload)

        emb = query_embedding or self.embedder.embed_query(query)
        self._redis.setex(f"{PREFIX}:sem:{exact_id}:vec", ttl, json.dumps(emb))
        self._redis.setex(f"{PREFIX}:sem:{exact_id}:payload", ttl, payload)
        self._redis.sadd(f"{PREFIX}:sem:ids", exact_id)
        logger.info("cache_store", key=exact_id, ttl=ttl)
