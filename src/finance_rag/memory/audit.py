"""Append-only audit trail.

Episodic memory and the compliance record are the same table. For a tax
advisory product the question "what did the system tell this client on 3 March,
and from which sources" has to be answerable months later, which means recording
the retrieved set, the cited set and the model identities -- floating aliases
like ``gpt-4o`` move under you, so an answer cannot be reconstructed without
pinning what actually produced it.

It doubles as the eval feedback source: a hand-written golden set measures
regressions, real traffic measures quality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from finance_rag.db import connection
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class AuditRecord:
    trace_id: str
    query: str
    thread_id: str | None = None
    user_id: str | None = None
    org_id: str | None = None
    rewritten_query: str | None = None
    service_line: str | None = None
    answer: str | None = None
    refused: bool = False
    refusal_reason: str | None = None
    retrieved_ids: list[str] = field(default_factory=list)
    cited_ids: list[str] = field(default_factory=list)
    hallucinated_ids: list[str] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)
    route: str | None = None
    critic_attempts: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    guardrails: list[str] = field(default_factory=list)
    latency_ms: float | None = None


_INSERT = text(
    """
    INSERT INTO query_audit (
        trace_id, thread_id, user_id, org_id, query, rewritten_query,
        service_line, answer, refused, refusal_reason, retrieved_ids,
        cited_ids, hallucinated_ids, models, route, critic_attempts,
        metrics, guardrails, latency_ms
    ) VALUES (
        :trace_id, :thread_id, :user_id, :org_id, :query, :rewritten_query,
        :service_line, :answer, :refused, :refusal_reason, :retrieved_ids,
        :cited_ids, :hallucinated_ids, CAST(:models AS jsonb), :route,
        :critic_attempts, CAST(:metrics AS jsonb), :guardrails, :latency_ms
    )
    RETURNING audit_id
    """
)


def write_audit(record: AuditRecord) -> int | None:
    """Persist one interaction. Never raises.

    An audit failure must not fail the user's request -- the answer has already
    been produced, and losing the record is a smaller harm than losing the
    answer. The failure is logged loudly so the gap is visible.
    """
    try:
        with connection() as conn:
            row = conn.execute(
                _INSERT,
                {
                    "trace_id": record.trace_id,
                    "thread_id": record.thread_id,
                    "user_id": record.user_id,
                    "org_id": record.org_id,
                    "query": record.query,
                    "rewritten_query": record.rewritten_query,
                    "service_line": record.service_line,
                    "answer": record.answer,
                    "refused": record.refused,
                    "refusal_reason": record.refusal_reason,
                    "retrieved_ids": list(record.retrieved_ids),
                    "cited_ids": list(record.cited_ids),
                    "hallucinated_ids": list(record.hallucinated_ids),
                    "models": json.dumps(record.models, default=str),
                    "route": record.route,
                    "critic_attempts": record.critic_attempts,
                    "metrics": json.dumps(record.metrics, default=str),
                    "guardrails": list(record.guardrails),
                    "latency_ms": record.latency_ms,
                },
            ).one()
            return int(row[0])
    except Exception as exc:  # noqa: BLE001
        logger.error("audit_write_failed", trace_id=record.trace_id, error=str(exc))
        return None


def recent_audits(limit: int = 20, thread_id: str | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT audit_id, trace_id, thread_id, query, refused, route,
               critic_attempts, cardinality(cited_ids) AS n_cited,
               cardinality(hallucinated_ids) AS n_hallucinated,
               latency_ms, created_at
        FROM query_audit
        {where}
        ORDER BY created_at DESC
        LIMIT :limit
    """.format(where="WHERE thread_id = :thread_id" if thread_id else "")
    params: dict[str, Any] = {"limit": limit}
    if thread_id:
        params["thread_id"] = thread_id
    with connection() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]
