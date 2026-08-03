"""Retrieval quality metrics for online monitoring and offline eval."""

from __future__ import annotations

import math
from typing import Sequence

from finance_rag.models import RetrievedChunk, RetrievalMetrics


def average_precision(relevances: Sequence[float], cutoff: int | None = None) -> float:
    vals = list(relevances[:cutoff] if cutoff else relevances)
    if not vals:
        return 0.0
    hits = 0.0
    sum_prec = 0.0
    for i, rel in enumerate(vals, start=1):
        if rel > 0:
            hits += 1
            sum_prec += hits / i
    return sum_prec / hits if hits else 0.0


def dcg(relevances: Sequence[float], k: int | None = None) -> float:
    vals = list(relevances[:k] if k else relevances)
    return sum(rel / math.log2(i + 1) for i, rel in enumerate(vals, start=1))


def ndcg(relevances: Sequence[float], k: int | None = None) -> float:
    vals = list(relevances[:k] if k else relevances)
    ideal = sorted(vals, reverse=True)
    ideal_dcg = dcg(ideal, k)
    if ideal_dcg == 0:
        return 0.0
    return dcg(vals, k) / ideal_dcg


def mrr(relevances: Sequence[float]) -> float:
    for i, rel in enumerate(relevances, start=1):
        if rel > 0:
            return 1.0 / i
    return 0.0


def hit_rate(relevances: Sequence[float], threshold: float = 0.0) -> float:
    return 1.0 if any(r > threshold for r in relevances) else 0.0


def compute_online_metrics(
    retrieved: list[RetrievedChunk], latency_ms: float, relevance_threshold: float = 0.35
) -> RetrievalMetrics:
    # Proxy relevance from fused/rerank scores for online dashboards
    relevances = [1.0 if r.score >= relevance_threshold else 0.0 for r in retrieved]
    graded = [max(r.score, 0.0) for r in retrieved]
    avg = sum(graded) / len(graded) if graded else 0.0
    return RetrievalMetrics(
        hit_rate=hit_rate(relevances),
        mrr=mrr(relevances),
        ndcg=ndcg(graded, k=min(10, len(graded))) if graded else 0.0,
        latency_ms=latency_ms,
        num_retrieved=len(retrieved),
        avg_relevance=avg,
    )


def evaluate_labeled(
    retrieved_ids: list[str],
    relevant_ids: set[str],
    scores: list[float] | None = None,
) -> RetrievalMetrics:
    binary = [1.0 if cid in relevant_ids else 0.0 for cid in retrieved_ids]
    graded = scores or binary
    return RetrievalMetrics(
        hit_rate=hit_rate(binary),
        mrr=mrr(binary),
        ndcg=ndcg(graded, k=10),
        context_precision=average_precision(binary),
        context_recall=(
            len(set(retrieved_ids) & relevant_ids) / len(relevant_ids) if relevant_ids else 0.0
        ),
        num_retrieved=len(retrieved_ids),
        avg_relevance=sum(graded) / len(graded) if graded else 0.0,
    )
