"""Offline evaluation against the labelled golden set.

Reports label-backed retrieval quality, citation grounding and abstention
correctness, broken down by service line, and can fail the build on regression.

    python scripts/run_eval.py                       # full run, writes a report
    python scripts/run_eval.py --no-judge            # skip LLM judging (cheaper)
    python scripts/run_eval.py --baseline data/eval/baseline.json
    python scripts/run_eval.py --save-baseline

Requires an indexed corpus and an OpenAI key; it issues real model calls.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from finance_rag.agent import MultiAgentRAG
from finance_rag.evaluation import diff_runs, list_runs, record_run
from finance_rag.logging_setup import configure_logging, get_logger
from finance_rag.metrics import citation_metrics, evaluate_labeled, judge_answer
from finance_rag.models import RAGResponse
from finance_rag.observability import record_eval_run

logger = get_logger(__name__)

GOLDEN_PATH = Path("data/eval/golden_set.json")
REPORT_PATH = Path("data/processed/eval_report.json")
DEFAULT_BASELINE = Path("data/eval/baseline.json")

# A regression beyond this on any headline metric fails the run. Tight enough to
# catch real damage, loose enough to absorb LLM non-determinism.
REGRESSION_TOLERANCE = 0.05


def _source_key(value: str) -> str:
    """Normalise a source to its comparable form (basename, lowercased)."""
    return Path(str(value)).name.strip().lower()


def _chunk_matches_label(chunk_meta: dict, doc_id: str, labels: set[str]) -> bool:
    """Does a retrieved chunk belong to one of the labelled documents?

    Labels are source basenames or corpus-supplied doc_ids. Generated doc_ids
    are path hashes and are never labelled directly, so the source is the join
    key for file-backed documents.
    """
    if doc_id and doc_id.strip().lower() in labels:
        return True
    source = chunk_meta.get("source")
    return bool(source) and _source_key(source) in labels


def _looks_refused(response: RAGResponse) -> bool:
    if response.refused:
        return True
    text = (response.answer or "").lower()
    markers = (
        "do not have sufficiently relevant",
        "insufficient",
        "cannot process this request",
        "not enough information",
        "does not contain",
    )
    return any(m in text for m in markers)


def evaluate_case(agent: MultiAgentRAG, case: dict, use_judge: bool) -> dict[str, Any]:
    query = case["query"]
    labels = {str(s).strip().lower() for s in case.get("relevant_sources", [])}
    expect_refusal = bool(case.get("expect_refusal"))

    response = agent.ask(query, service_line=None)
    refused = _looks_refused(response)

    citations = response.citations
    # Retrieval is scored over what retrieval returned. Scoring it over
    # citations would let refusal behaviour move the retrieval numbers: a
    # refusal reports every retrieved chunk as a citation, an answer reports
    # only the subset it cited.
    retrieved_ids = list(response.retrieved_ids) or [c.chunk_id for c in citations]
    by_id = {c.chunk_id: c for c in citations}
    relevant_ids = {
        cid
        for cid in retrieved_ids
        if (c := by_id.get(cid))
        and _chunk_matches_label({"source": c.source}, c.doc_id, labels)
    }

    row: dict[str, Any] = {
        "id": case.get("id"),
        "query": query,
        "service_line": case.get("service_line"),
        "expect_refusal": expect_refusal,
        "refused": refused,
        "confidence": response.confidence,
        "top_cosine": response.metrics.top_cosine,
        "latency_ms": response.metrics.latency_ms,
        "cache_hit": response.cache_hit,
    }

    if expect_refusal:
        # Ranking metrics are meaningless with no relevant document. What matters
        # is whether the system declined instead of inventing an answer.
        row["abstention_correct"] = refused
        row["hallucinated_citations"] = response.metrics.hallucinated_citations
        return row

    row["abstention_correct"] = not refused  # answering was the correct action

    labeled = evaluate_labeled(
        retrieved_ids=retrieved_ids,
        relevant_ids=relevant_ids,
        k=len(retrieved_ids) or 1,
        total_relevant=max(len(labels), 1),
    )
    row.update(
        {
            "hit_rate": labeled.hit_rate,
            "mrr": labeled.mrr,
            "ndcg": labeled.ndcg,
            "precision_at_k": labeled.precision_at_k,
            "recall_at_k": labeled.recall_at_k,
        }
    )

    grounding = citation_metrics(
        cited_ids=[c.chunk_id for c in citations],
        retrieved_ids=retrieved_ids,
        relevant_ids=relevant_ids,
    )
    row["citation_precision"] = grounding.get("citation_precision")
    row["hallucinated_citations"] = grounding.get("hallucinated_citations")

    if use_judge and not refused:
        # Full passage text, not the display excerpt.
        judged = judge_answer(query, response.answer, [c.text or c.excerpt for c in citations][:8])
        row["faithfulness"] = judged.faithfulness
        row["answer_relevance"] = judged.answer_relevance

    return row


def _mean(values: list[Any]) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return round(statistics.fmean(nums), 4) if nums else None


def summarise(rows: list[dict]) -> dict[str, Any]:
    answerable = [r for r in rows if not r["expect_refusal"]]
    refusable = [r for r in rows if r["expect_refusal"]]

    summary: dict[str, Any] = {
        "n_cases": len(rows),
        "n_answerable": len(answerable),
        "n_refusal_cases": len(refusable),
        "hit_rate": _mean([r.get("hit_rate") for r in answerable]),
        "mrr": _mean([r.get("mrr") for r in answerable]),
        "ndcg": _mean([r.get("ndcg") for r in answerable]),
        "precision_at_k": _mean([r.get("precision_at_k") for r in answerable]),
        "recall_at_k": _mean([r.get("recall_at_k") for r in answerable]),
        "citation_precision": _mean([r.get("citation_precision") for r in answerable]),
        "faithfulness": _mean([r.get("faithfulness") for r in answerable]),
        "answer_relevance": _mean([r.get("answer_relevance") for r in answerable]),
        "abstention_accuracy": _mean([r.get("abstention_correct") for r in rows]),
        "refusal_recall": _mean([r.get("abstention_correct") for r in refusable]),
        "latency_ms_p50": None,
        "total_hallucinated_citations": sum(
            len(r.get("hallucinated_citations") or []) for r in rows
        ),
    }

    latencies = [r["latency_ms"] for r in rows if isinstance(r.get("latency_ms"), (int, float))]
    if latencies:
        summary["latency_ms_p50"] = round(statistics.median(latencies), 1)

    by_line: dict[str, dict] = {}
    grouped = defaultdict(list)
    for r in answerable:
        grouped[r.get("service_line") or "unlabelled"].append(r)
    for line, group in sorted(grouped.items()):
        by_line[line] = {
            "n": len(group),
            "hit_rate": _mean([r.get("hit_rate") for r in group]),
            "mrr": _mean([r.get("mrr") for r in group]),
            "recall_at_k": _mean([r.get("recall_at_k") for r in group]),
        }
    summary["by_service_line"] = by_line
    return summary


GATED_METRICS = ("hit_rate", "mrr", "ndcg", "recall_at_k", "abstention_accuracy")


def check_regression(summary: dict, baseline: dict) -> list[str]:
    failures = []
    for metric in GATED_METRICS:
        now, was = summary.get(metric), baseline.get(metric)
        if now is None or was is None:
            continue
        if now < was - REGRESSION_TOLERANCE:
            failures.append(
                f"{metric}: {now:.4f} < baseline {was:.4f} - {REGRESSION_TOLERANCE}"
            )
    if summary.get("total_hallucinated_citations", 0) > baseline.get(
        "total_hallucinated_citations", 0
    ):
        failures.append(
            f"hallucinated citations rose to {summary['total_hallucinated_citations']} "
            f"from {baseline.get('total_hallucinated_citations', 0)}"
        )
    return failures


def _print_runs(runs: list[dict]) -> None:
    if not runs:
        print("no recorded runs")
        return
    print(f"{'run':>4}  {'sha':<10} {'pass':<5} {'hit':>6} {'mrr':>6} {'ndcg':>6} "
          f"{'absten':>7} {'halluc':>6}  label")
    def f(row: dict, key: str) -> str:
        v = row.get(key)
        return f"{v:.3f}" if isinstance(v, (int, float)) else "  -  "

    for r in runs:
        sha = (r["git_sha"] or "-") + ("*" if r["git_dirty"] else "")
        passed = {True: "pass", False: "FAIL", None: "-"}[r["passed"]]
        print(f"{r['run_id']:>4}  {sha:<10} {passed:<5} {f(r, 'hit_rate'):>6} "
              f"{f(r, 'mrr'):>6} {f(r, 'ndcg'):>6} "
              f"{f(r, 'abstention_accuracy'):>7} "
              f"{r['hallucinated_citations']:>6}  {r['label'] or ''}")


def _print_diff(d: dict) -> None:
    print(f"run {d['base_run']} -> run {d['head_run']}\n")
    print(f"{'metric':<24}{'base':>10}{'head':>10}{'delta':>10}")
    for name, v in d["metrics"].items():
        if v["base"] is None and v["head"] is None:
            continue
        b = f"{v['base']:.4f}" if v["base"] is not None else "-"
        h = f"{v['head']:.4f}" if v["head"] is not None else "-"
        delta = f"{v['delta']:+.4f}" if v["delta"] is not None else "-"
        print(f"{name:<24}{b:>10}{h:>10}{delta:>10}")

    if d["config_changes"]:
        print("\nconfig changes:")
        for k, v in sorted(d["config_changes"].items()):
            print(f"  {k}: {v['base']!r} -> {v['head']!r}")

    if d["changed_cases"]:
        # The case-level half is the point: an average that moved says to go
        # looking, a named case that flipped says where.
        print(f"\ncases that changed ({len(d['changed_cases'])}):")
        for c in d["changed_cases"]:
            moved = f"refused {c['base_refused']}->{c['head_refused']}"
            hits = f"hit {c['base_hit']}->{c['head_hit']}"
            print(f"  {c['case_id']:<12} {moved:<26} {hits}")
    else:
        print("\nno case-level changes")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the FinanceRAG golden-set evaluation")
    parser.add_argument("--no-judge", action="store_true", help="skip LLM judging")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="evaluate only the first N cases")
    parser.add_argument("--label", default=None, help="name this run in the history table")
    parser.add_argument("--list-runs", action="store_true", help="show recent runs and exit")
    parser.add_argument(
        "--diff", nargs=2, type=int, metavar=("BASE", "HEAD"),
        help="compare two recorded runs and exit",
    )
    args = parser.parse_args()

    # History queries need no model calls, so they short-circuit before the
    # agent is constructed.
    if args.list_runs:
        _print_runs(list_runs())
        return 0
    if args.diff:
        _print_diff(diff_runs(args.diff[0], args.diff[1]))
        return 0

    configure_logging()
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    cases = payload["cases"] if isinstance(payload, dict) else payload
    if args.limit:
        cases = cases[: args.limit]

    # No thread_id: each eval case must be independent. Sharing conversation
    # memory across cases would let one answer contaminate the next.
    agent = MultiAgentRAG()
    rows = [evaluate_case(agent, case, use_judge=not args.no_judge) for case in cases]
    summary = summarise(rows)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps({"summary": summary, "cases": rows}, indent=2), encoding="utf-8"
    )

    print(json.dumps(summary, indent=2))
    print(f"\nreport: {REPORT_PATH}")

    if args.save_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        run_id = record_run(summary, rows, label=args.label or "baseline", passed=True)
        record_eval_run(run_id, summary, label=args.label or "baseline")
        print(f"baseline written: {args.baseline}" + (f" (run {run_id})" if run_id else ""))
        return 0

    if args.baseline.exists():
        failures = check_regression(summary, json.loads(args.baseline.read_text(encoding="utf-8")))
        run_id = record_run(summary, rows, label=args.label, passed=not failures,
                            failures=failures)
        record_eval_run(run_id, summary, label=args.label)
        if run_id:
            print(f"recorded as run {run_id}", flush=True)
        if failures:
            # Written to both streams on purpose. stderr is unbuffered while
            # stdout is block-buffered when piped, so a stderr-only verdict can
            # surface far from the report it belongs to -- and be discarded
            # outright by a `| tail`. Note also that piping this script masks its
            # exit status: a shell pipeline reports the last command's code.
            verdict = ["", "REGRESSION:"] + [f"  - {f}" for f in failures]
            for stream in (sys.stdout, sys.stderr):
                print("\n".join(verdict), file=stream, flush=True)
            return 1
        print("\nno regression against baseline", flush=True)
    else:
        print(f"\nno baseline at {args.baseline}; run with --save-baseline to create one")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
