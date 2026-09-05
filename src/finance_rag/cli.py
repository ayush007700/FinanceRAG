"""CLI entrypoints for indexing and local Q&A."""

from __future__ import annotations

import argparse
import json

from finance_rag.agent import FinanceRAGAgent
from finance_rag.logging_setup import configure_logging
from finance_rag.pipeline import index_corpus


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="finance-rag")
    sub = parser.add_subparsers(dest="command", required=True)

    index_p = sub.add_parser("index", help="Ingest and index corpus into Postgres")
    index_p.add_argument("paths", nargs="+", help="Files or directories to index")

    # Executed by the indexing ECS task. It updates the job row itself, so the
    # row records what happened rather than what was dispatched.
    job_p = sub.add_parser("run-job", help="Execute a queued indexing job by id")
    job_p.add_argument("job_id", type=int)

    reap_p = sub.add_parser("reap-jobs", help="Fail jobs abandoned by a killed worker")
    reap_p.add_argument("--older-than-minutes", type=int, default=60)

    ask_p = sub.add_parser("ask", help="Ask a question against the indexed corpus")
    ask_p.add_argument("query")
    ask_p.add_argument("--service-line", default=None)

    args = parser.parse_args()
    if args.command == "index":
        stats = index_corpus(args.paths)
        print(json.dumps(stats, indent=2))
    elif args.command == "run-job":
        from finance_rag.pipeline.jobs import get_job, run_job

        run_job(args.job_id)
        job = get_job(args.job_id) or {}
        print(json.dumps({k: str(v) for k, v in job.items()}, indent=2))
        # Non-zero exit so the ECS task, and any pipeline waiting on it, sees
        # the failure rather than reading success from a stopped container.
        if job.get("status") != "succeeded":
            raise SystemExit(1)
    elif args.command == "reap-jobs":
        from finance_rag.pipeline.jobs import reap_stale_jobs

        print(json.dumps({"reaped": reap_stale_jobs(args.older_than_minutes)}))
    elif args.command == "ask":
        result = FinanceRAGAgent().ask(args.query, service_line=args.service_line)
        print(json.dumps(
            {
                "answer": result.answer,
                "confidence": result.confidence,
                "refused": result.refused,
                "citations": [c.__dict__ for c in result.citations],
                "metrics": result.metrics.__dict__,
                "guardrails": result.guardrails,
                "trace_id": result.trace_id,
            },
            indent=2,
        ))


if __name__ == "__main__":
    main()
