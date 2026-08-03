"""Offline retrieval evaluation against golden questions."""

from __future__ import annotations

import json
from pathlib import Path

from finance_rag.agent import FinanceRAGAgent
from finance_rag.logging_setup import configure_logging
from finance_rag.metrics import evaluate_labeled, judge_answer


def main() -> None:
    configure_logging()
    golden = json.loads(Path("data/eval/golden_set.json").read_text(encoding="utf-8"))
    agent = FinanceRAGAgent()

    rows = []
    for item in golden:
        result = agent.ask(item["query"])
        retrieved_ids = [c.doc_id for c in result.citations]
        # Soft match on title/source keywords from notes/doc ids
        relevant = set(item.get("relevant_doc_ids", []))
        soft_hits = []
        for cid, citation in zip(retrieved_ids, result.citations, strict=False):
            blob = f"{cid} {citation.title} {citation.source}".lower()
            soft_hits.append(1.0 if any(r.lower() in blob for r in relevant) else 0.0)

        labeled = evaluate_labeled(
            retrieved_ids=[c.chunk_id for c in result.citations],
            relevant_ids={
                c.chunk_id
                for c, hit in zip(result.citations, soft_hits, strict=False)
                if hit
            },
            scores=[c.score for c in result.citations],
        )
        judged = judge_answer(
            item["query"],
            result.answer,
            [c.excerpt for c in result.citations],
        )
        rows.append(
            {
                "query": item["query"],
                "hit_rate": labeled.hit_rate,
                "mrr": labeled.mrr,
                "ndcg": labeled.ndcg,
                "faithfulness": judged.faithfulness,
                "answer_relevance": judged.answer_relevance,
                "confidence": result.confidence,
                "refused": result.refused,
            }
        )

    out = Path("data/processed/eval_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
