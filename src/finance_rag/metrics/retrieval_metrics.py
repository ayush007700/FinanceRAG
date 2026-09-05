"""Retrieval quality metrics.

The split here is deliberate:

*Offline* metrics (``evaluate_labeled``) need relevance labels. They answer
"did retrieval find the right passages" and are the only place nDCG, MRR,
precision and recall mean anything.

*Online* metrics (``compute_online_telemetry``) have no labels. They can only
describe what the system did -- latency, how many passages came back, how
similar they were, whether the answer was refused. They deliberately do **not**
report hit rate, MRR or nDCG.

The previous implementation reported all three online by thresholding the
retriever's own scores. That is circular -- the retriever grading its own
homework -- and because the candidate list arrives pre-sorted, the "ideal"
ranking always equalled the actual one, pinning nDCG to exactly 1.0 on every
request. A metric that cannot vary is worse than no metric: it looks like
evidence while carrying none.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from finance_rag.models import RetrievalMetrics, RetrievedChunk


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


def ndcg(
    relevances: Sequence[float],
    k: int | None = None,
    ideal_relevances: Sequence[float] | None = None,
) -> float:
    """Normalised DCG at k.

    The ideal ranking is built from the *full* relevance set before truncation,
    not from the top-k slice. Deriving it from the slice hides any relevant
    document ranked past k, which inflates the score -- ``ndcg([1, 0, 3], k=2)``
    scores 1.0 that way, when the achievable ideal top-2 is ``[3, 1]`` and the
    true value is 0.275.

    Pass ``ideal_relevances`` when relevant documents exist that retrieval missed
    entirely; otherwise they are invisible to the ideal and recall failures score
    as perfect ranking.
    """
    vals = list(relevances[:k] if k else relevances)
    pool = list(ideal_relevances) if ideal_relevances is not None else list(relevances)
    ideal = sorted(pool, reverse=True)[: (k if k else len(pool))]

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


def precision_at_k(relevances: Sequence[float], k: int) -> float:
    vals = list(relevances[:k])
    if not vals:
        return 0.0
    return sum(1.0 for r in vals if r > 0) / len(vals)


def recall_at_k(relevances: Sequence[float], total_relevant: int, k: int) -> float:
    """Fraction of all known-relevant documents retrieved within the top k.

    ``total_relevant`` counts the labelled relevant documents in the corpus, not
    just those retrieved -- otherwise recall is 1.0 by construction.
    """
    if total_relevant <= 0:
        return 0.0
    found = sum(1.0 for r in list(relevances[:k]) if r > 0)
    return min(found / total_relevant, 1.0)


def compute_online_telemetry(
    retrieved: list[RetrievedChunk],
    latency_ms: float,
    refused: bool = False,
) -> RetrievalMetrics:
    """Per-request observability. No labels, therefore no quality claims.

    Cosine is the only absolute signal available at request time, so the
    distribution of cosines is what gets reported. hit_rate / mrr / ndcg stay
    ``None`` to mark them as unmeasured rather than perfect.
    """
    cosines = [r.cosine for r in retrieved]
    reranks = [r.rerank_score for r in retrieved if r.rerank_score is not None]

    return RetrievalMetrics(
        hit_rate=None,
        mrr=None,
        ndcg=None,
        latency_ms=latency_ms,
        num_retrieved=len(retrieved),
        top_cosine=max(cosines) if cosines else None,
        mean_cosine=(sum(cosines) / len(cosines)) if cosines else None,
        min_cosine=min(cosines) if cosines else None,
        top_rerank_score=max(reranks) if reranks else None,
        avg_relevance=(sum(cosines) / len(cosines)) if cosines else None,
        refused=refused,
    )


def evaluate_labeled(
    retrieved_ids: list[str],
    relevant_ids: set[str],
    scores: list[float] | None = None,
    k: int | None = None,
    total_relevant: int | None = None,
) -> RetrievalMetrics:
    """Offline metrics against relevance labels.

    ``scores`` are ignored for ranking quality on purpose: graded relevance must
    come from labels, not from the retriever's confidence in itself.
    """
    binary = [1.0 if cid in relevant_ids else 0.0 for cid in retrieved_ids]
    cutoff = k or len(retrieved_ids) or 1
    known_relevant = total_relevant if total_relevant is not None else len(relevant_ids)

    # Relevant documents retrieval missed entirely still belong in the ideal
    # ranking, otherwise a recall failure scores as a perfect one.
    missed = max(known_relevant - int(sum(binary)), 0)
    ideal_pool = binary + [1.0] * missed

    return RetrievalMetrics(
        hit_rate=hit_rate(binary),
        mrr=mrr(binary),
        ndcg=ndcg(binary, k=cutoff, ideal_relevances=ideal_pool),
        precision_at_k=precision_at_k(binary, cutoff),
        recall_at_k=recall_at_k(binary, known_relevant, cutoff),
        context_precision=average_precision(binary),
        context_recall=(
            len(set(retrieved_ids) & relevant_ids) / known_relevant if known_relevant else 0.0
        ),
        num_retrieved=len(retrieved_ids),
        avg_relevance=(sum(scores) / len(scores)) if scores else None,
    )


def citation_metrics(
    cited_ids: Sequence[str],
    retrieved_ids: Sequence[str],
    relevant_ids: set[str] | None = None,
) -> dict[str, float | list[str]]:
    """Grounding quality of the citations the model actually emitted.

    ``hallucinated`` are cited ids absent from the retrieved set -- ids the model
    invented. In an advisory product these matter more than ranking metrics: a
    fabricated citation is an answer that cannot be audited.
    """
    cited = list(dict.fromkeys(cited_ids))
    retrieved = set(retrieved_ids)

    hallucinated = [cid for cid in cited if cid not in retrieved]
    grounded = [cid for cid in cited if cid in retrieved]

    result: dict[str, float | list[str]] = {
        "citation_grounding": (len(grounded) / len(cited)) if cited else 0.0,
        "hallucinated_citations": hallucinated,
        "num_cited": float(len(cited)),
    }

    if relevant_ids is not None:
        correct = [cid for cid in grounded if cid in relevant_ids]
        result["citation_precision"] = (len(correct) / len(cited)) if cited else 0.0
        result["citation_recall"] = (
            len(set(correct) & relevant_ids) / len(relevant_ids) if relevant_ids else 0.0
        )
    return result


# Backwards-compatible alias. The old name claimed to compute quality metrics;
# the new one says what it actually does.
compute_online_metrics = compute_online_telemetry
