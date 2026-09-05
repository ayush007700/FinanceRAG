"""Golden-set evaluation harness.

Exercised against a stub agent so the scoring, label-resolution and regression
logic are verified without model calls or an indexed corpus.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from finance_rag.models import Citation, RAGResponse, RetrievalMetrics

_SPEC = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"
)
run_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_eval)


def _citation(chunk_id: str, source: str, doc_id: str = "hash123") -> Citation:
    return Citation(
        chunk_id=chunk_id,
        doc_id=doc_id,
        title="T",
        source=source,
        excerpt="short excerpt",
        score=0.8,
        text="the full passage text that the generator actually saw",
    )


class _StubAgent:
    def __init__(self, response: RAGResponse):
        self._response = response
        self.calls: list[str] = []

    def ask(self, query, service_line=None, **kwargs):
        self.calls.append(query)
        return self._response


def _response(citations, answer="The four-part test applies.", refused=False) -> RAGResponse:
    return RAGResponse(
        answer=answer,
        citations=citations,
        confidence=0.7,
        metrics=RetrievalMetrics(top_cosine=0.7, latency_ms=120.0, num_retrieved=len(citations)),
        refused=refused,
    )


# --------------------------------------------------------------------------
# label resolution
# --------------------------------------------------------------------------


def test_labels_match_on_source_basename_not_generated_doc_id():
    """File-backed doc_ids are path hashes, so the source is the join key."""
    assert run_eval._chunk_matches_label(
        {"source": "data/corpus/rd_tax_credit.md"}, "49ad70e0fd2b88cd", {"rd_tax_credit.md"}
    )


def test_labels_match_on_corpus_supplied_doc_id():
    """JSON-sourced documents carry stable, human-written ids."""
    assert run_eval._chunk_matches_label(
        {"source": "source-advisors-knowledge"}, "lifo-001", {"lifo-001"}
    )


def test_label_matching_is_case_and_path_insensitive():
    """Regression: the path is produced by the indexing machine, not the eval one.

    A corpus indexed on Windows stores backslash sources. Evaluated on Linux,
    ``Path(...).name`` treats the whole string as one filename, so every label
    match fails and the report shows total retrieval failure for a system that
    is working correctly. CI runs on Linux, which is exactly where it bit.
    """
    for source in (
        "D:\\Deeplearning\\data\\RD_Tax_Credit.MD",  # indexed on Windows
        "/srv/data/corpus/rd_tax_credit.md",         # indexed on Linux
        "data/corpus/RD_Tax_Credit.md",              # relative
    ):
        assert run_eval._chunk_matches_label(
            {"source": source}, "x", {"rd_tax_credit.md"}
        ), source


def test_unrelated_document_does_not_match():
    assert not run_eval._chunk_matches_label(
        {"source": "data/corpus/cost_segregation.md"}, "abc", {"rd_tax_credit.md"}
    )


# --------------------------------------------------------------------------
# refusal detection
# --------------------------------------------------------------------------


def test_explicit_refusal_flag_is_honoured():
    assert run_eval._looks_refused(_response([], refused=True))


def test_refusal_detected_from_answer_text():
    r = _response([], answer="I do not have sufficiently relevant knowledge to answer.")
    assert run_eval._looks_refused(r)


def test_normal_answer_is_not_a_refusal():
    assert not run_eval._looks_refused(_response([_citation("c1", "rd_tax_credit.md")]))


# --------------------------------------------------------------------------
# case scoring
# --------------------------------------------------------------------------


def test_answerable_case_scores_a_correct_retrieval():
    agent = _StubAgent(_response([_citation("c1", "data/corpus/rd_tax_credit.md")]))
    row = run_eval.evaluate_case(
        agent,
        {"id": "rd-001", "query": "four-part test?", "relevant_sources": ["rd_tax_credit.md"]},
        use_judge=False,
    )
    assert row["hit_rate"] == 1.0
    assert row["mrr"] == pytest.approx(1.0)
    assert row["abstention_correct"] is True
    assert row["hallucinated_citations"] == []


def test_answerable_case_scores_a_wrong_retrieval():
    agent = _StubAgent(_response([_citation("c1", "data/corpus/cost_segregation.md")]))
    row = run_eval.evaluate_case(
        agent,
        {"id": "rd-001", "query": "four-part test?", "relevant_sources": ["rd_tax_credit.md"]},
        use_judge=False,
    )
    assert row["hit_rate"] == 0.0
    assert row["recall_at_k"] == 0.0


def test_refusal_case_rewards_declining():
    agent = _StubAgent(_response([], answer="I do not have sufficiently relevant knowledge.",
                                 refused=True))
    row = run_eval.evaluate_case(
        agent,
        {"id": "refuse-001", "query": "capital of France?", "relevant_sources": [],
         "expect_refusal": True},
        use_judge=False,
    )
    assert row["abstention_correct"] is True
    assert "hit_rate" not in row  # ranking metrics are meaningless here


def test_refusal_case_penalises_answering_anyway():
    """The hallucination case: confidently answering what the corpus cannot support."""
    agent = _StubAgent(_response([_citation("c1", "rd_tax_credit.md")],
                                 answer="The rate is 14%."))
    row = run_eval.evaluate_case(
        agent,
        {"id": "refuse-003", "query": "exact credit rate for 2025?", "relevant_sources": [],
         "expect_refusal": True},
        use_judge=False,
    )
    assert row["abstention_correct"] is False


def test_judge_is_not_invoked_when_disabled():
    agent = _StubAgent(_response([_citation("c1", "rd_tax_credit.md")]))
    row = run_eval.evaluate_case(
        agent, {"query": "q", "relevant_sources": ["rd_tax_credit.md"]}, use_judge=False
    )
    assert "faithfulness" not in row


# --------------------------------------------------------------------------
# summary + regression gate
# --------------------------------------------------------------------------


def _rows():
    return [
        {"expect_refusal": False, "service_line": "R&D Tax Credit", "hit_rate": 1.0,
         "mrr": 1.0, "ndcg": 1.0, "recall_at_k": 1.0, "abstention_correct": True,
         "latency_ms": 100.0, "hallucinated_citations": []},
        {"expect_refusal": False, "service_line": "Cost Segregation", "hit_rate": 0.0,
         "mrr": 0.0, "ndcg": 0.0, "recall_at_k": 0.0, "abstention_correct": True,
         "latency_ms": 200.0, "hallucinated_citations": []},
        {"expect_refusal": True, "abstention_correct": False, "latency_ms": 150.0,
         "hallucinated_citations": ["GHOST"]},
    ]


def test_summary_separates_answerable_from_refusal_cases():
    s = run_eval.summarise(_rows())
    assert s["n_cases"] == 3
    assert s["n_answerable"] == 2
    assert s["n_refusal_cases"] == 1
    assert s["hit_rate"] == pytest.approx(0.5)          # refusal cases excluded
    assert s["refusal_recall"] == pytest.approx(0.0)
    assert s["abstention_accuracy"] == pytest.approx(2 / 3, abs=1e-4)  # _mean rounds to 4dp


def test_summary_breaks_down_by_service_line():
    s = run_eval.summarise(_rows())
    assert s["by_service_line"]["R&D Tax Credit"]["hit_rate"] == 1.0
    assert s["by_service_line"]["Cost Segregation"]["hit_rate"] == 0.0


def test_summary_counts_hallucinated_citations():
    assert run_eval.summarise(_rows())["total_hallucinated_citations"] == 1


def test_summary_reports_median_latency():
    assert run_eval.summarise(_rows())["latency_ms_p50"] == pytest.approx(150.0)


def test_regression_gate_passes_within_tolerance():
    base = {"hit_rate": 0.80, "mrr": 0.80, "ndcg": 0.80, "recall_at_k": 0.80,
            "abstention_accuracy": 0.9, "total_hallucinated_citations": 0}
    now = dict(base, hit_rate=0.77)  # inside the 0.05 tolerance
    assert run_eval.check_regression(now, base) == []


def test_regression_gate_fails_on_a_real_drop():
    base = {"hit_rate": 0.80, "mrr": 0.80, "ndcg": 0.80, "recall_at_k": 0.80,
            "abstention_accuracy": 0.9, "total_hallucinated_citations": 0}
    now = dict(base, hit_rate=0.50)
    failures = run_eval.check_regression(now, base)
    assert len(failures) == 1
    assert "hit_rate" in failures[0]


def test_regression_gate_fails_on_any_new_hallucination():
    """Zero tolerance: a fabricated citation is never an acceptable regression."""
    base = {"total_hallucinated_citations": 0}
    failures = run_eval.check_regression({"total_hallucinated_citations": 1}, base)
    assert len(failures) == 1
    assert "hallucinated" in failures[0]


def test_regression_gate_ignores_unmeasured_metrics():
    base = {"hit_rate": 0.8, "faithfulness": 0.9, "total_hallucinated_citations": 0}
    now = {"hit_rate": 0.8, "faithfulness": None, "total_hallucinated_citations": 0}
    assert run_eval.check_regression(now, base) == []


# --------------------------------------------------------------------------
# golden set integrity
# --------------------------------------------------------------------------


def test_golden_set_is_well_formed():
    payload = json.loads(Path("data/eval/golden_set.json").read_text(encoding="utf-8"))
    cases = payload["cases"]
    ids = [c["id"] for c in cases]

    assert len(ids) == len(set(ids)), "duplicate case ids"
    for c in cases:
        assert c["query"].strip()
        if c.get("expect_refusal"):
            assert c["relevant_sources"] == [], f"{c['id']}: refusal case must have no labels"
        else:
            assert c["relevant_sources"], f"{c['id']}: answerable case needs at least one label"


def test_golden_set_labels_resolve_to_real_corpus_documents():
    """Every label must name a document that actually exists.

    A label that matches nothing scores as a permanent retrieval failure and
    quietly drags the whole benchmark down.
    """
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    from finance_rag.ingestion import ingest_paths

    docs = ingest_paths([Path("data/corpus")])
    known = {Path(m.source).name.lower() for _, m in docs} | {m.doc_id.lower() for _, m in docs}

    payload = json.loads(Path("data/eval/golden_set.json").read_text(encoding="utf-8"))
    unresolved = {
        c["id"]: [s for s in c["relevant_sources"] if s.lower() not in known]
        for c in payload["cases"]
    }
    unresolved = {k: v for k, v in unresolved.items() if v}
    assert not unresolved, f"golden set references unknown documents: {unresolved}"
