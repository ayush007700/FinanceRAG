"""Metric correctness.

Every test here pins a bug that shipped: metrics that could not vary, an nDCG
that inflated, and a citation check that never checked anything.
"""

from __future__ import annotations

import pytest

from finance_rag.metrics import (
    citation_metrics,
    compute_online_telemetry,
    evaluate_labeled,
    ndcg,
    precision_at_k,
    recall_at_k,
)
from finance_rag.models import Chunk, RetrievedChunk


def _rc(cid: str, cosine: float, rerank: float | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(chunk_id=cid, doc_id="d", text="t", index=0, tokens=1),
        score=0.03,
        cosine=cosine,
        rerank_score=rerank,
    )


# --------------------------------------------------------------------------
# nDCG
# --------------------------------------------------------------------------


def test_ndcg_ideal_is_not_taken_from_the_truncated_slice():
    """Regression: a relevant doc ranked past k must still shape the ideal.

    Building the ideal from relevances[:k] hides rank 3's relevance, scoring a
    poor ranking as perfect.
    """
    assert ndcg([1, 0, 3], k=2) == pytest.approx(0.275, abs=1e-3)


def test_ndcg_of_a_genuinely_ideal_ranking_is_one():
    assert ndcg([3, 2, 1]) == pytest.approx(1.0)


def test_ndcg_of_a_reversed_ranking_is_below_one():
    assert ndcg([1, 2, 3]) < 1.0


def test_ndcg_accounts_for_relevant_documents_never_retrieved():
    """Relevant documents retrieval missed belong in the ideal ranking.

    Only observable when the cutoff has room for them: nDCG@k judges ordering
    *within* k, so at k=1 a single correct hit is genuinely ideal no matter how
    many documents were missed. Coverage beyond the cutoff is recall@k's job,
    not nDCG's -- which is why the eval reports both.
    """
    found_one_of_one = ndcg([1.0], k=3, ideal_relevances=[1.0])
    found_one_of_three = ndcg([1.0], k=3, ideal_relevances=[1.0, 1.0, 1.0])
    assert found_one_of_one == pytest.approx(1.0)
    assert found_one_of_three == pytest.approx(0.469, abs=1e-3)
    assert found_one_of_three < found_one_of_one


def test_ndcg_at_k_cannot_see_past_the_cutoff():
    """Documented limitation, pinned so nobody 'fixes' it into nonsense."""
    assert ndcg([1.0], k=1, ideal_relevances=[1.0, 1.0, 1.0]) == pytest.approx(1.0)


def test_ndcg_all_irrelevant_is_zero():
    assert ndcg([0, 0, 0], k=3) == 0.0


# --------------------------------------------------------------------------
# precision / recall
# --------------------------------------------------------------------------


def test_precision_at_k():
    assert precision_at_k([1, 0, 1, 0], 4) == pytest.approx(0.5)
    assert precision_at_k([1, 0, 1, 0], 2) == pytest.approx(0.5)
    assert precision_at_k([], 3) == 0.0


def test_recall_uses_total_relevant_not_just_what_was_retrieved():
    """Regression: dividing by retrieved-relevant makes recall 1.0 always."""
    assert recall_at_k([1, 0, 0], total_relevant=4, k=3) == pytest.approx(0.25)
    assert recall_at_k([1, 1, 0], total_relevant=2, k=3) == pytest.approx(1.0)
    assert recall_at_k([1], total_relevant=0, k=1) == 0.0


# --------------------------------------------------------------------------
# online telemetry
# --------------------------------------------------------------------------


def test_online_telemetry_reports_no_label_backed_metrics():
    """Regression: these were self-graded and pinned to 1.0 on every request.

    None means "not measured". Reporting a number here would be fabrication --
    there are no relevance labels at request time.
    """
    m = compute_online_telemetry([_rc("a", 0.9), _rc("b", 0.4)], latency_ms=12.0)
    assert m.hit_rate is None
    assert m.mrr is None
    assert m.ndcg is None


def test_online_telemetry_reports_the_absolute_signal():
    m = compute_online_telemetry([_rc("a", 0.9), _rc("b", 0.3)], latency_ms=12.0)
    assert m.top_cosine == pytest.approx(0.9)
    assert m.min_cosine == pytest.approx(0.3)
    assert m.mean_cosine == pytest.approx(0.6)
    assert m.num_retrieved == 2
    assert m.latency_ms == 12.0


def test_online_telemetry_varies_with_retrieval_quality():
    """The old metrics were constant; these must actually move."""
    good = compute_online_telemetry([_rc("a", 0.88)], latency_ms=1.0)
    bad = compute_online_telemetry([_rc("a", 0.05)], latency_ms=1.0)
    assert good.top_cosine > bad.top_cosine


def test_online_telemetry_handles_empty_retrieval():
    m = compute_online_telemetry([], latency_ms=5.0, refused=True)
    assert m.num_retrieved == 0
    assert m.top_cosine is None
    assert m.refused is True


def test_online_telemetry_surfaces_rerank_scores():
    m = compute_online_telemetry([_rc("a", 0.5, rerank=0.83)], latency_ms=1.0)
    assert m.top_rerank_score == pytest.approx(0.83)


# --------------------------------------------------------------------------
# labelled evaluation
# --------------------------------------------------------------------------


def test_evaluate_labeled_rewards_a_correct_ranking():
    m = evaluate_labeled(["a", "b", "c"], {"a"}, k=3, total_relevant=1)
    assert m.hit_rate == 1.0
    assert m.mrr == pytest.approx(1.0)
    assert m.recall_at_k == pytest.approx(1.0)


def test_evaluate_labeled_penalises_a_late_hit():
    early = evaluate_labeled(["a", "x", "y"], {"a"}, k=3, total_relevant=1)
    late = evaluate_labeled(["x", "y", "a"], {"a"}, k=3, total_relevant=1)
    assert late.mrr < early.mrr
    assert late.ndcg < early.ndcg


def test_evaluate_labeled_scores_a_complete_miss_as_zero():
    m = evaluate_labeled(["x", "y"], {"a"}, k=2, total_relevant=1)
    assert m.hit_rate == 0.0
    assert m.mrr == 0.0
    assert m.ndcg == 0.0
    assert m.recall_at_k == 0.0


# --------------------------------------------------------------------------
# citation grounding
# --------------------------------------------------------------------------


def test_citation_metrics_flags_fabricated_ids():
    r = citation_metrics(cited_ids=["a", "GHOST"], retrieved_ids=["a", "b"])
    assert r["hallucinated_citations"] == ["GHOST"]
    assert r["citation_grounding"] == pytest.approx(0.5)


def test_citation_metrics_perfect_grounding():
    r = citation_metrics(cited_ids=["a", "b"], retrieved_ids=["a", "b", "c"])
    assert r["hallucinated_citations"] == []
    assert r["citation_grounding"] == pytest.approx(1.0)


def test_citation_metrics_deduplicates_repeated_ids():
    r = citation_metrics(cited_ids=["a", "a", "a"], retrieved_ids=["a"])
    assert r["num_cited"] == 1.0


def test_citation_metrics_with_no_citations():
    r = citation_metrics(cited_ids=[], retrieved_ids=["a"])
    assert r["citation_grounding"] == 0.0
    assert r["num_cited"] == 0.0


def test_citation_precision_against_labels():
    r = citation_metrics(
        cited_ids=["a", "b"], retrieved_ids=["a", "b"], relevant_ids={"a"}
    )
    assert r["citation_precision"] == pytest.approx(0.5)
    assert r["citation_recall"] == pytest.approx(1.0)
