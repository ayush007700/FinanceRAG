"""Prometheus + CloudWatch monitoring helpers."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)

try:
    from prometheus_client import Counter, Histogram

    REQUESTS = Counter(
        "finance_rag_requests_total",
        "Total RAG requests",
        ["endpoint", "status"],
    )
    LATENCY = Histogram(
        "finance_rag_latency_seconds",
        "RAG request latency",
        ["endpoint"],
        buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    )
    RETRIEVAL_COSINE = Histogram(
        "finance_rag_top_cosine",
        "Top absolute cosine similarity of retrieved context",
        buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    )
    HALLUCINATED_CITATIONS = Counter(
        "finance_rag_hallucinated_citations_total",
        "Citation ids emitted by the model that were not in the retrieved set",
    )
    GUARDRAIL_BLOCKS = Counter(
        "finance_rag_guardrail_blocks_total",
        "Guardrail blocks",
        ["stage"],
    )
except Exception:  # noqa: BLE001
    REQUESTS = LATENCY = RETRIEVAL_COSINE = GUARDRAIL_BLOCKS = None
    HALLUCINATED_CITATIONS = None


def emit_cloudwatch_metric(name: str, value: float, unit: str = "None", dimensions: dict | None = None) -> None:
    settings = get_settings()
    if settings.app_env == "development":
        logger.info("metric", name=name, value=value, unit=unit, dimensions=dimensions or {})
        return
    try:
        import boto3

        cw = boto3.client("cloudwatch", region_name=settings.aws_region)
        metric = {
            "MetricName": name,
            "Timestamp": datetime.now(UTC),
            "Value": value,
            "Unit": unit,
        }
        if dimensions:
            metric["Dimensions"] = [{"Name": k, "Value": str(v)} for k, v in dimensions.items()]
        cw.put_metric_data(Namespace=settings.aws_cloudwatch_namespace, MetricData=[metric])
    except Exception as exc:  # noqa: BLE001
        logger.warning("cloudwatch_emit_failed", error=str(exc))


@contextmanager
def track_request(endpoint: str) -> Iterator[dict]:
    start = time.perf_counter()
    status = "ok"
    meta: dict = {}
    try:
        yield meta
    except Exception:
        status = "error"
        raise
    finally:
        elapsed = time.perf_counter() - start
        if REQUESTS is not None:
            REQUESTS.labels(endpoint=endpoint, status=status).inc()
            LATENCY.labels(endpoint=endpoint).observe(elapsed)
        emit_cloudwatch_metric(
            "RequestLatencyMs",
            elapsed * 1000,
            unit="Milliseconds",
            dimensions={"Endpoint": endpoint, "Status": status},
        )
        if meta.get("top_cosine") is not None and RETRIEVAL_COSINE is not None:
            RETRIEVAL_COSINE.observe(float(meta["top_cosine"]))
        if meta.get("hallucinated_citations") and HALLUCINATED_CITATIONS is not None:
            HALLUCINATED_CITATIONS.inc(int(meta["hallucinated_citations"]))
        if meta.get("refused") and GUARDRAIL_BLOCKS is not None:
            GUARDRAIL_BLOCKS.labels(stage="response").inc()
