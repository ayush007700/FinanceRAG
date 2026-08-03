"""Prometheus + CloudWatch monitoring helpers."""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

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
    RETRIEVAL_HIT = Counter(
        "finance_rag_retrieval_hit_total",
        "Retrieval hit / miss",
        ["hit"],
    )
    GUARDRAIL_BLOCKS = Counter(
        "finance_rag_guardrail_blocks_total",
        "Guardrail blocks",
        ["stage"],
    )
except Exception:  # noqa: BLE001
    REQUESTS = LATENCY = RETRIEVAL_HIT = GUARDRAIL_BLOCKS = None


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
            "Timestamp": datetime.now(timezone.utc),
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
        if meta.get("hit_rate") is not None and RETRIEVAL_HIT is not None:
            RETRIEVAL_HIT.labels(hit=str(bool(meta["hit_rate"]))).inc()
        if meta.get("refused") and GUARDRAIL_BLOCKS is not None:
            GUARDRAIL_BLOCKS.labels(stage="response").inc()
