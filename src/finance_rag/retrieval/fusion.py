"""Reciprocal Rank Fusion.

    RRF(d) = sum over rankers r of  w_r / (k + rank_r(d))

``rank_r(d)`` is d's 1-based position in ranker r's output. Documents missing
from a ranker simply contribute nothing for that ranker.

The production hot path evaluates this inside Postgres (see
``PgVectorStore.hybrid_search_rrf``) so that dense and sparse fusion costs one
round trip. These helpers implement the identical formula in Python for the
rankers that are fused after that query returns -- entity expansion today -- and
give the fusion rule a directly testable form.

Why rank-based fusion rather than a weighted blend of scores:

* Ranker outputs are not commensurable. ``ts_rank_cd`` is unbounded, cosine
  lives in [-1, 1]; adding them is a category error.
* Min-max normalising first is worse than useless: it pins the best candidate to
  1.0 and the worst to 0.0 *whatever* their absolute quality, so a query that
  retrieved nothing relevant becomes indistinguishable from one that retrieved
  perfect matches.
* Ranks are immune to scale, outliers and distribution shape, and need no tuned
  mixing coefficient.

The cost is that RRF discards magnitude: a runaway best match scores no higher
than a marginal one, and top-1 is always ~1/(k+1). Absolute judgements must
therefore read a real similarity, never a fused score.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

DEFAULT_K = 60


def rrf_contribution(rank: int, k: int = DEFAULT_K, weight: float = 1.0) -> float:
    """Score one ranker contributes to a document at 1-based ``rank``.

    ``k`` flattens the head of the curve. At k=60 the gap between rank 1 and
    rank 2 is ~1.6%, so appearing in several rankers outweighs topping one; at
    k=0 rank 1 is worth double rank 2 and a single confident ranker dominates.
    """
    if rank < 1:
        raise ValueError(f"rank is 1-based, got {rank}")
    return weight / (k + rank)


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]],
    k: int = DEFAULT_K,
    weights: Mapping[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse named ranked lists of document ids into one ranking.

    Args:
        rankings: ranker name -> ranked document ids, best first.
        k: RRF smoothing constant.
        weights: optional per-ranker weight; defaults to 1.0 each.

    Returns:
        ``(doc_id, score)`` pairs sorted by descending fused score. Ties break on
        doc id so the ordering is deterministic.
    """
    weights = weights or {}
    scores: dict[str, float] = {}
    for ranker, doc_ids in rankings.items():
        weight = weights.get(ranker, 1.0)
        for rank, doc_id in enumerate(doc_ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + rrf_contribution(
                rank, k=k, weight=weight
            )
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
