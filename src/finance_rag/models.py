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
    """A retrieved chunk plus the signals that produced its ranking.

    ``score`` is the fused RRF total (or the reranker's score once reranking has
    run) and is only meaningful *relative* to the other candidates. ``cosine`` is
    the raw, un-normalised similarity and is the only field safe to compare
    against an absolute threshold, e.g. for abstention.
    """

    chunk: Chunk
    score: float
    cosine: float = 0.0
    rrf_score: float = 0.0
    vector_score: float = 0.0
    bm25_score: float = 0.0
    graph_score: float = 0.0
    rerank_score: float | None = None
    dense_rank: int | None = None
    sparse_rank: int | None = None
    graph_rank: int | None = None


@dataclass
class Citation:
    chunk_id: str
    doc_id: str
    title: str
    source: str
    excerpt: str
    score: float
    # Full passage text. Kept off the API payload (see api.app.citation_payload)
    # but required by offline judging: grading faithfulness against a 280-char
    # excerpt marks claims unsupported because the evidence was truncated away.
    text: str = ""


@dataclass
class RetrievalMetrics:
    """Retrieval measurements.

    Label-backed fields (hit_rate, mrr, ndcg, precision/recall) are ``None`` on
    the online path, where no ground truth exists. ``None`` means "not
    measured" and must not be read as zero or as success -- these were
    previously computed from the retriever's own scores, which pinned them to
    1.0 on every request.
    """

    # Offline only: require relevance labels.
    hit_rate: float | None = None
    mrr: float | None = None
    ndcg: float | None = None
    precision_at_k: float | None = None
    recall_at_k: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    # LLM-judged, offline.
    faithfulness: float | None = None
    answer_relevance: float | None = None

    # Grounding: measurable online, since it only needs the retrieved set.
    citation_grounding: float | None = None
    citation_precision: float | None = None
    citation_recall: float | None = None
    hallucinated_citations: list[str] = field(default_factory=list)

    # Telemetry: always available.
    latency_ms: float | None = None
    num_retrieved: int = 0
    top_cosine: float | None = None
    mean_cosine: float | None = None
    min_cosine: float | None = None
    top_rerank_score: float | None = None
    avg_relevance: float | None = None
    refused: bool = False


@dataclass
class GuardrailResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    sanitized_text: str | None = None
    risk_score: float = 0.0
    cited_ids: list[str] = field(default_factory=list)
    grounded_ids: list[str] = field(default_factory=list)
    hallucinated_citations: list[str] = field(default_factory=list)


@dataclass
class RAGResponse:
    answer: str
    citations: list[Citation]
    confidence: float
    metrics: RetrievalMetrics
    guardrails: list[str] = field(default_factory=list)
    refused: bool = False
    trace_id: str | None = None
    cache_hit: bool = False
    cache_layer: str | None = None
    # Chunk ids retrieval returned, independent of what the answer cited.
    # Retrieval quality must be measured against this, never against citations:
    # a refused answer reports every retrieved chunk as a citation while an
    # answered one reports only the subset it cited, so scoring retrieval over
    # citations lets generation behaviour move retrieval metrics.
    retrieved_ids: list[str] = field(default_factory=list)
