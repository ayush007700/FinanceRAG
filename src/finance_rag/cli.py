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

    ask_p = sub.add_parser("ask", help="Ask a question against the indexed corpus")
    ask_p.add_argument("query")
    ask_p.add_argument("--service-line", default=None)

    args = parser.parse_args()
    if args.command == "index":
        stats = index_corpus(args.paths)
        print(json.dumps(stats, indent=2))
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
