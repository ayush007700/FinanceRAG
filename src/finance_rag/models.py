from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DocumentMeta:
    doc_id: str
    source: str
    title: str
    service_line: str | None = None
    jurisdiction: str | None = None
    doc_type: str = "policy"
    effective_date: str | None = None
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    index: int
    tokens: int
    section: str | None = None
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None


@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float
    vector_score: float = 0.0
    bm25_score: float = 0.0
    graph_score: float = 0.0
    rerank_score: float | None = None


@dataclass
class Citation:
    chunk_id: str
    doc_id: str
    title: str
    source: str
    excerpt: str
    score: float


@dataclass
class RetrievalMetrics:
    hit_rate: float | None = None
    mrr: float | None = None
    ndcg: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None
    faithfulness: float | None = None
    answer_relevance: float | None = None
    latency_ms: float | None = None
    num_retrieved: int = 0
    avg_relevance: float | None = None


@dataclass
class GuardrailResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    sanitized_text: str | None = None
    risk_score: float = 0.0


@dataclass
class RAGResponse:
    answer: str
    citations: list[Citation]
    confidence: float
    metrics: RetrievalMetrics
    guardrails: list[str] = field(default_factory=list)
    refused: bool = False
    trace_id: str | None = None
