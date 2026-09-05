"""Self-hosted Langfuse tracing.

LangSmith already traces this system, but it is SaaS: every query and every
retrieved passage leaves the deployment. For tax advisory content that needs
sign-off, and it is the same concern raised for the reranker. Langfuse runs
in-VPC, so the traces stay where the data does.

The two coexist deliberately -- they are not redundant. LangSmith is the hosted
convenience for development; Langfuse is the one that can be pointed at
production. Either can be disabled independently.

Everything here fails open. Observability that can take down the service it
observes is a liability, so a missing key, an unreachable host or a broken
handler degrades to no tracing rather than to no answers.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


@lru_cache
def _client() -> Any | None:
    """Process-wide Langfuse client, or None when unconfigured."""
    settings = get_settings()
    if not settings.langfuse_enabled:
        return None
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        logger.warning(
            "langfuse_unconfigured",
            reason="LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set",
        )
        return None
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            timeout=settings.langfuse_timeout_seconds,
        )
        logger.info("langfuse_ready", host=settings.langfuse_host)
        return client
    except Exception as exc:  # noqa: BLE001
        logger.warning("langfuse_unavailable", error=str(exc))
        return None


def is_enabled() -> bool:
    return _client() is not None


def callback_handler() -> Any | None:
    """LangChain/LangGraph callback that records each node as a span.

    Attached to ``graph.invoke``, so the trace mirrors the agent topology --
    supervisor, researcher, analyst, critic each appear as their own span with
    their own latency, which is what makes a slow or looping request readable.
    """
    if _client() is None:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as exc:  # noqa: BLE001
        logger.warning("langfuse_handler_failed", error=str(exc))
        return None


def check_connection() -> bool:
    """Verify credentials and reachability. Used by /health, never on the hot path."""
    client = _client()
    if client is None:
        return False
    try:
        return bool(client.auth_check())
    except Exception as exc:  # noqa: BLE001
        logger.warning("langfuse_auth_check_failed", error=str(exc))
        return False


def record_eval_run(run_id: int | None, summary: dict[str, Any], label: str | None = None) -> None:
    """Publish evaluation metrics as Langfuse scores.

    The Postgres eval_runs table remains the system of record; this is the
    comparison UI on top of it. Scores are keyed by the eval run id so a chart
    in Langfuse and a row in Postgres refer to the same run.
    """
    client = _client()
    if client is None or not summary:
        return

    trace_name = f"eval-run-{run_id}" if run_id else "eval-run"
    numeric = {
        k: v
        for k, v in summary.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    try:
        for name, value in numeric.items():
            client.create_score(
                name=f"eval.{name}",
                value=float(value),
                comment=label or trace_name,
                metadata={"eval_run_id": run_id, "label": label},
            )
        client.flush()
        logger.info("langfuse_eval_recorded", run_id=run_id, scores=len(numeric))
    except Exception as exc:  # noqa: BLE001
        # An observability failure must never fail the eval.
        logger.warning("langfuse_eval_record_failed", error=str(exc))


def flush() -> None:
    """Drain buffered events. Langfuse batches, so a short-lived process must flush."""
    client = _client()
    if client is None:
        return
    try:
        client.flush()
    except Exception as exc:  # noqa: BLE001
        logger.debug("langfuse_flush_failed", error=str(exc))
