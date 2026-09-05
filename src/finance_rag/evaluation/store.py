"""Persist and compare evaluation runs.

Replaces copying eval_report.json by hand. A run records the metrics, the
configuration that produced them, and the commit -- because a metric without its
configuration is a number, not a measurement.

Case-level rows are stored too, since "which question broke" is the question
that leads to a fix, while "the average moved" only tells you to go looking.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from finance_rag.config import get_settings
from finance_rag.db import connection
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)

# Knobs that change retrieval or generation behaviour. Deliberately explicit:
# dumping every setting would bury the ones that matter and churn the diff on
# unrelated changes.
TRACKED_SETTINGS = (
    "openai_chat_model",
    "openai_fast_model",
    "openai_embedding_model",
    "openai_embedding_dimensions",
    "rerank_provider",
    "cohere_rerank_model",
    "rerank_top_k",
    "retrieval_top_k",
    "rrf_k",
    "rrf_candidates",
    "graph_expand_weight",
    "chunk_size",
    "chunk_overlap",
    "min_absolute_cosine",
    "answerability_check_enabled",
    "critic_enabled",
    "critic_max_retries",
    "web_search_enabled",
)

HEADLINE = (
    "hit_rate",
    "mrr",
    "ndcg",
    "precision_at_k",
    "recall_at_k",
    "abstention_accuracy",
    "refusal_recall",
    "faithfulness",
    "latency_ms_p50",
)


@dataclass
class RunRef:
    run_id: int
    label: str | None
    git_sha: str | None
    created_at: Any


def config_snapshot() -> dict[str, Any]:
    settings = get_settings()
    return {name: getattr(settings, name, None) for name in TRACKED_SETTINGS}


def git_state() -> tuple[str | None, bool]:
    """Current commit and whether the tree is dirty.

    A dirty tree means the numbers cannot be reproduced from the SHA alone, so
    it is recorded rather than silently implied.
    """
    def _run(*args: str) -> str | None:
        try:
            return subprocess.run(
                args, capture_output=True, text=True, timeout=5, check=True
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return None

    sha = _run("git", "rev-parse", "--short", "HEAD")
    status = _run("git", "status", "--porcelain")
    return sha, bool(status)


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def record_run(
    summary: dict[str, Any],
    cases: list[dict[str, Any]],
    label: str | None = None,
    baseline_run_id: int | None = None,
    passed: bool | None = None,
    failures: list[str] | None = None,
) -> int | None:
    """Persist one evaluation run. Never raises.

    A storage failure must not fail the eval: the numbers are already computed
    and printed, and losing the history row is the smaller loss.
    """
    sha, dirty = git_state()
    try:
        with connection() as conn:
            run_id = conn.execute(
                text(
                    """
                    INSERT INTO eval_runs (
                        label, git_sha, git_dirty, config, summary,
                        n_cases, n_answerable, n_refusal_cases,
                        hit_rate, mrr, ndcg, precision_at_k, recall_at_k,
                        abstention_accuracy, refusal_recall, faithfulness,
                        latency_ms_p50, hallucinated_citations,
                        baseline_run_id, passed, failures
                    ) VALUES (
                        :label, :git_sha, :git_dirty, CAST(:config AS jsonb),
                        CAST(:summary AS jsonb),
                        :n_cases, :n_answerable, :n_refusal_cases,
                        :hit_rate, :mrr, :ndcg, :precision_at_k, :recall_at_k,
                        :abstention_accuracy, :refusal_recall, :faithfulness,
                        :latency_ms_p50, :hallucinated_citations,
                        :baseline_run_id, :passed, :failures
                    ) RETURNING run_id
                    """
                ),
                {
                    "label": label,
                    "git_sha": sha,
                    "git_dirty": dirty,
                    "config": json.dumps(config_snapshot(), default=str),
                    "summary": json.dumps(summary, default=str),
                    "n_cases": int(summary.get("n_cases") or 0),
                    "n_answerable": int(summary.get("n_answerable") or 0),
                    "n_refusal_cases": int(summary.get("n_refusal_cases") or 0),
                    **{k: _num(summary.get(k)) for k in HEADLINE},
                    "hallucinated_citations": int(
                        summary.get("total_hallucinated_citations") or 0
                    ),
                    "baseline_run_id": baseline_run_id,
                    "passed": passed,
                    "failures": list(failures or []),
                },
            ).scalar_one()

            rows = [
                {
                    "run_id": run_id,
                    "case_id": str(c.get("id") or ""),
                    "query": c.get("query") or "",
                    "service_line": c.get("service_line"),
                    "expect_refusal": bool(c.get("expect_refusal")),
                    "refused": bool(c.get("refused")),
                    "abstention_correct": c.get("abstention_correct"),
                    "hit_rate": _num(c.get("hit_rate")),
                    "mrr": _num(c.get("mrr")),
                    "ndcg": _num(c.get("ndcg")),
                    "precision_at_k": _num(c.get("precision_at_k")),
                    "recall_at_k": _num(c.get("recall_at_k")),
                    "faithfulness": _num(c.get("faithfulness")),
                    "top_cosine": _num(c.get("top_cosine")),
                    "latency_ms": _num(c.get("latency_ms")),
                    "hallucinated_ids": list(c.get("hallucinated_citations") or []),
                }
                for c in cases
                if c.get("id")
            ]
            if rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO eval_cases (
                            run_id, case_id, query, service_line, expect_refusal,
                            refused, abstention_correct, hit_rate, mrr, ndcg,
                            precision_at_k, recall_at_k, faithfulness, top_cosine,
                            latency_ms, hallucinated_ids
                        ) VALUES (
                            :run_id, :case_id, :query, :service_line, :expect_refusal,
                            :refused, :abstention_correct, :hit_rate, :mrr, :ndcg,
                            :precision_at_k, :recall_at_k, :faithfulness, :top_cosine,
                            :latency_ms, :hallucinated_ids
                        )
                        ON CONFLICT (run_id, case_id) DO NOTHING
                        """
                    ),
                    rows,
                )
        logger.info("eval_run_recorded", run_id=run_id, git_sha=sha, cases=len(rows))
        return int(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("eval_run_record_failed", error=str(exc))
        return None


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    """
                    SELECT run_id, label, git_sha, git_dirty, passed,
                           hit_rate, mrr, ndcg, recall_at_k, abstention_accuracy,
                           refusal_recall, hallucinated_citations, latency_ms_p50,
                           n_cases, created_at
                    FROM eval_runs ORDER BY run_id DESC LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings()
        ]


def diff_runs(base_id: int, head_id: int) -> dict[str, Any]:
    """Compare two runs at summary and case level.

    The case-level half is the point: an average that moved 0.02 says nothing,
    while "ee-005 went from answered to refused" says exactly where to look.
    """
    with connection() as conn:
        runs = {
            r["run_id"]: dict(r)
            for r in conn.execute(
                text("SELECT * FROM eval_runs WHERE run_id IN (:a, :b)"),
                {"a": base_id, "b": head_id},
            ).mappings()
        }
        if base_id not in runs or head_id not in runs:
            missing = [i for i in (base_id, head_id) if i not in runs]
            raise ValueError(f"unknown run id(s): {missing}")

        metrics = {
            name: {
                "base": _num(runs[base_id].get(name)),
                "head": _num(runs[head_id].get(name)),
            }
            for name in HEADLINE
        }
        for value in metrics.values():
            value["delta"] = (
                round(value["head"] - value["base"], 4)
                if value["base"] is not None and value["head"] is not None
                else None
            )

        changed = [
            dict(r)
            for r in conn.execute(
                text(
                    """
                    SELECT b.case_id, b.query,
                           b.refused AS base_refused, h.refused AS head_refused,
                           b.hit_rate AS base_hit,   h.hit_rate AS head_hit
                    FROM eval_cases b
                    JOIN eval_cases h
                      ON h.case_id = b.case_id AND h.run_id = :head
                    WHERE b.run_id = :base
                      AND (b.refused IS DISTINCT FROM h.refused
                           OR b.hit_rate IS DISTINCT FROM h.hit_rate)
                    ORDER BY b.case_id
                    """
                ),
                {"base": base_id, "head": head_id},
            ).mappings()
        ]

        base_cfg = runs[base_id].get("config") or {}
        head_cfg = runs[head_id].get("config") or {}
        config_changes = {
            k: {"base": base_cfg.get(k), "head": head_cfg.get(k)}
            for k in set(base_cfg) | set(head_cfg)
            if base_cfg.get(k) != head_cfg.get(k)
        }

    return {
        "base_run": base_id,
        "head_run": head_id,
        "metrics": metrics,
        "config_changes": config_changes,
        "changed_cases": changed,
    }
