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


_cw_client = None


def _cloudwatch():
    """One client per process. boto3 client construction loads service models
    and resolves credentials; doing that on every request added tens of
    milliseconds to each answer for no reason."""
    global _cw_client
    if _cw_client is None:
        import boto3

        _cw_client = boto3.client("cloudwatch", region_name=get_settings().aws_region)
    return _cw_client


def _datum(name: str, value: float, unit: str, dimensions: dict | None) -> dict:
    metric: dict = {
        "MetricName": name,
        "Timestamp": datetime.now(UTC),
        "Value": value,
        "Unit": unit,
    }
    if dimensions:
        metric["Dimensions"] = [{"Name": k, "Value": str(v)} for k, v in dimensions.items()]
    return metric


def emit_cloudwatch_metrics(data: list[dict]) -> None:
    """Publish a batch in one call. Each datum comes from :func:`_datum`.

    One PutMetricData per request rather than one per metric: the API accepts
    up to a thousand data points per call, and the request path should not pay
    a round trip per number it wants to remember.
    """
    if not data:
        return
    settings = get_settings()
    if settings.app_env == "development":
        for m in data:
            logger.info("metric", name=m["MetricName"], value=m["Value"], unit=m["Unit"])
        return
    try:
        _cloudwatch().put_metric_data(Namespace=settings.aws_cloudwatch_namespace, MetricData=data)
    except Exception as exc:  # noqa: BLE001
        # Metrics are diagnostics; failing to record one must never fail the
        # request it describes.
        logger.warning("cloudwatch_emit_failed", error=str(exc), count=len(data))


def emit_cloudwatch_metric(name: str, value: float, unit: str = "None", dimensions: dict | None = None) -> None:
    """Single-metric convenience over :func:`emit_cloudwatch_metrics`."""
    emit_cloudwatch_metrics([_datum(name, value, unit, dimensions)])


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
        dims = {"Endpoint": endpoint}

        # Prometheus: in-process, pulled by whatever scrapes /metrics. In the
        # AWS deployment nothing does, so these are for local docker compose.
        if REQUESTS is not None:
            REQUESTS.labels(endpoint=endpoint, status=status).inc()
            LATENCY.labels(endpoint=endpoint).observe(elapsed)
        if meta.get("top_cosine") is not None and RETRIEVAL_COSINE is not None:
            RETRIEVAL_COSINE.observe(float(meta["top_cosine"]))
        if meta.get("hallucinated_citations") and HALLUCINATED_CITATIONS is not None:
            HALLUCINATED_CITATIONS.inc(int(meta["hallucinated_citations"]))
        if meta.get("refused") and GUARDRAIL_BLOCKS is not None:
            GUARDRAIL_BLOCKS.labels(stage="response").inc()

        # CloudWatch: pushed, and the source of truth in AWS. The same four
        # signals as above, so the quality metrics are not Prometheus-only --
        # they were, which meant retrieval confidence, refusals and
        # hallucinated citations were invisible in the deployment that
        # mattered. Latency carries Status; the quality metrics do not, since
        # a refusal on an errored request is not a data point.
        batch = [
            _datum("RequestLatencyMs", elapsed * 1000, "Milliseconds", {**dims, "Status": status}),
            _datum("Requests", 1, "Count", {**dims, "Status": status}),
        ]
        if meta.get("top_cosine") is not None:
            batch.append(_datum("TopCosine", float(meta["top_cosine"]), "None", dims))
        if meta.get("hallucinated_citations"):
            batch.append(
                _datum("HallucinatedCitations", int(meta["hallucinated_citations"]), "Count", dims)
            )
        if meta.get("refused"):
            batch.append(_datum("Refusals", 1, "Count", dims))
        emit_cloudwatch_metrics(batch)
