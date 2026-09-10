"""OpenTelemetry setup.

The OTel packages were dependencies long before anything imported them. That is
worse than not having them: a reviewer greps for `opentelemetry`, finds five
requirements and no tracing, and cannot tell whether it was abandoned or never
started. This module is the answer either way.

What it adds over what already exists: LangSmith and Langfuse trace the *agent*
-- prompts, tokens, tool choices -- and Prometheus counts requests. Neither
shows where wall-clock time went inside one HTTP request, across the retrieval
SQL, the rerank call and the generation call. That is the gap OTel fills, and it
is the one that matters when a p99 moves and the model provider is not at fault.

**Disabled unless an endpoint is configured.** No collector means no exporter
and no span processor, so the cost is one import and a no-op tracer. Turning it
on is an environment variable, not a deploy of different code -- the same
property that makes it useful in an incident.
"""

from __future__ import annotations

from typing import Any

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)

_configured = False


def setup_tracing(app: Any | None = None) -> bool:
    """Configure the tracer provider and instrument FastAPI.

    Returns whether tracing was enabled. Idempotent: the lifespan can run more
    than once in tests, and installing a second provider would silently orphan
    the spans from the first.
    """
    global _configured
    if _configured:
        return True

    settings = get_settings()
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    if not endpoint:
        logger.debug("tracing_disabled", reason="OTEL_EXPORTER_OTLP_ENDPOINT unset")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "deployment.environment": settings.app_env,
            }
        )
        provider = TracerProvider(resource=resource)
        # Batched rather than simple: a span export on the request path would
        # add the collector's latency to every response, and a collector that
        # is slow or down would then be an outage rather than a blind spot.
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
        )
        trace.set_tracer_provider(provider)

        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            # /health is the ALB check every 30s per task and carries no
            # information; /metrics is scraped on its own schedule. Excluding
            # them keeps the trace volume proportional to real traffic.
            FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")

        _configured = True
        logger.info("tracing_enabled", endpoint=endpoint)
        return True
    except Exception as exc:  # noqa: BLE001
        # Tracing is diagnostics. Failing to start it must never fail the
        # service it was meant to diagnose -- the same fail-open rule the other
        # observability paths follow.
        logger.warning("tracing_setup_failed", error=str(exc))
        return False


def get_tracer(name: str) -> Any:
    """A tracer for ``name``.

    Safe before :func:`setup_tracing`: the API returns a no-op tracer when no
    provider is installed, so call sites need no guard.
    """
    from opentelemetry import trace

    return trace.get_tracer(name)
