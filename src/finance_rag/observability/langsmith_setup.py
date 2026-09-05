"""LangSmith tracing bootstrap — works locally and on ECS via env vars."""

from __future__ import annotations

import os

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


def configure_langsmith() -> bool:
    """Enable LangSmith if LANGSMITH_TRACING=true and API key is set.

    Failures posting traces should not break RAG; we only set env flags here.
    Disable with LANGSMITH_TRACING=false if you see multipart ingest errors.
    """
    settings = get_settings()
    if not settings.langsmith_tracing or not settings.langsmith_api_key:
        os.environ.pop("LANGSMITH_TRACING", None)
        os.environ.pop("LANGCHAIN_TRACING_V2", None)
        logger.info("langsmith_disabled")
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    # Prefer background uploads; don't crash the request on trace failures.
    os.environ.setdefault("LANGCHAIN_CALLBACKS_BACKGROUND", "true")
    logger.info("langsmith_enabled", project=settings.langsmith_project)
    return True
