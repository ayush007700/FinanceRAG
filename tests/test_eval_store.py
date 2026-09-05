"""Evaluation run history.

Replaces comparing runs by copying eval_report.json -- data/processed/ still
holds four such copies from a single day's work. Integration tests skip without
a database; the pure logic is always exercised.
"""

from __future__ import annotations

import pytest

from finance_rag.evaluation import config_snapshot, git_state
from finance_rag.evaluation.store import HEADLINE, TRACKED_SETTINGS, _num


def _database_available() -> bool:
    try:
        from finance_rag.db import healthcheck

        return healthcheck()
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(not _database_available(), reason="Postgres not reachable")


# --------------------------------------------------------------------------
# configuration capture
# --------------------------------------------------------------------------


def test_config_snapshot_captures_the_knobs_that_change_behaviour():
    """A metric without its configuration is a number, not a measurement."""
    snap = config_snapshot()
    for key in ("rrf_k", "cohere_rerank_model", "openai_embedding_dimensions",
                "min_absolute_cosine", "critic_enabled"):
        assert key in snap


def test_tracked_settings_are_explicit_not_everything():
    """Dumping every setting would bury the ones that matter and churn diffs."""
    assert len(TRACKED_SETTINGS) < 30
    assert "openai_api_key" not in TRACKED_SETTINGS  # never persist secrets
    assert "cohere_api_key" not in TRACKED_SETTINGS
    assert "tavily_api_key" not in TRACKED_SETTINGS
    assert "database_url" not in TRACKED_SETTINGS


def test_git_state_returns_a_sha_and_dirty_flag():
    sha, dirty = git_state()
    assert sha is None or (isinstance(sha, str) and len(sha) >= 6)
    assert isinstance(dirty, bool)


def test_numeric_coercion_rejects_booleans():
    """A bool is an int in Python; storing True as 1.0 in a metric column lies."""
    assert _num(0.5) == 0.5
    assert _num(3) == 3.0
    assert _num(True) is None
    assert _num(None) is None
    assert _num("0.5") is None


def test_headline_metrics_have_columns():
    for name in ("hit_rate", "mrr", "ndcg", "recall_at_k", "abstention_accuracy"):
        assert name in HEADLINE


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def _summary(**overrides):
    base = {
        "n_cases": 3, "n_answerable": 2, "n_refusal_cases": 1,
        "hit_rate": 0.9, "mrr": 0.8, "ndcg": 0.85, "precision_at_k": 0.4,
        "recall_at_k": 0.9, "abstention_accuracy": 0.87, "refusal_recall": 1.0,
        "latency_ms_p50": 4200.0, "total_hallucinated_citations": 0,
    }
    base.update(overrides)
    return base


def _cases(refused_ids=()):
    return [
        {"id": "a-1", "query": "q1", "expect_refusal": False,
         "refused": "a-1" in refused_ids, "abstention_correct": "a-1" not in refused_ids,
         "hit_rate": 1.0, "mrr": 1.0, "latency_ms": 100.0, "hallucinated_citations": []},
        {"id": "a-2", "query": "q2", "expect_refusal": False,
         "refused": "a-2" in refused_ids, "abstention_correct": "a-2" not in refused_ids,
         "hit_rate": 0.0, "mrr": 0.0, "latency_ms": 120.0, "hallucinated_citations": []},
        {"id": "r-1", "query": "q3", "expect_refusal": True, "refused": True,
         "abstention_correct": True, "latency_ms": 90.0, "hallucinated_citations": []},
    ]


@pytest.fixture
def cleanup_runs():
    ids: list[int] = []
    yield ids
    if ids and _database_available():
        from sqlalchemy import text

        from finance_rag.db import connection

        with connection() as conn:
            conn.execute(
                text("DELETE FROM eval_runs WHERE run_id = ANY(:ids)"), {"ids": ids}
            )


@requires_db
def test_run_is_recorded_with_metrics_and_cases(cleanup_runs):
    from finance_rag.evaluation import list_runs, record_run

    run_id = record_run(_summary(), _cases(), label="unit-test")
    cleanup_runs.append(run_id)
    assert run_id

    row = next(r for r in list_runs(limit=50) if r["run_id"] == run_id)
    assert row["label"] == "unit-test"
    assert row["hit_rate"] == pytest.approx(0.9)
    assert row["n_cases"] == 3


@requires_db
def test_diff_reports_metric_deltas_and_changed_cases(cleanup_runs):
    """The case-level half is why this exists: an average says go looking, a
    named case that flipped says where."""
    from finance_rag.evaluation import diff_runs, record_run

    base = record_run(_summary(), _cases(), label="base")
    head = record_run(_summary(hit_rate=0.5), _cases(refused_ids={"a-2"}), label="head")
    cleanup_runs.extend([base, head])

    d = diff_runs(base, head)
    assert d["metrics"]["hit_rate"]["delta"] == pytest.approx(-0.4)
    changed = {c["case_id"] for c in d["changed_cases"]}
    assert "a-2" in changed
    assert "a-1" not in changed


@requires_db
def test_diff_surfaces_configuration_changes(cleanup_runs, monkeypatch):
    from finance_rag.config import get_settings
    from finance_rag.evaluation import diff_runs, record_run

    base = record_run(_summary(), _cases(), label="cfg-base")
    monkeypatch.setenv("RRF_K", "17")
    get_settings.cache_clear()
    head = record_run(_summary(), _cases(), label="cfg-head")
    get_settings.cache_clear()
    cleanup_runs.extend([base, head])

    changes = diff_runs(base, head)["config_changes"]
    assert changes["rrf_k"]["head"] == 17


@requires_db
def test_diff_rejects_unknown_run_ids():
    from finance_rag.evaluation import diff_runs

    with pytest.raises(ValueError, match="unknown run"):
        diff_runs(10**9, 10**9 + 1)


@requires_db
def test_identical_runs_report_no_case_changes(cleanup_runs):
    from finance_rag.evaluation import diff_runs, record_run

    a = record_run(_summary(), _cases(), label="same-a")
    b = record_run(_summary(), _cases(), label="same-b")
    cleanup_runs.extend([a, b])
    assert diff_runs(a, b)["changed_cases"] == []


def test_record_failure_returns_none_rather_than_raising(monkeypatch):
    """A storage failure must not fail the eval; the numbers are already computed."""
    from contextlib import contextmanager

    from finance_rag.evaluation import store

    @contextmanager
    def _broken():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(store, "connection", _broken)
    assert store.record_run(_summary(), _cases()) is None
