"""FastAPI application for Source Advisors FinanceRAG."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from finance_rag.agent import FinanceRAGAgent
from finance_rag.config import get_settings
from finance_rag.logging_setup import configure_logging, get_logger
from finance_rag.monitoring import track_request
from finance_rag.pipeline import index_corpus

configure_logging()
logger = get_logger(__name__)
settings = get_settings()

app = FastAPI(
    title="Source Advisors FinanceRAG",
    description="Advanced LangGraph + Neo4j RAG for specialized tax consulting",
    version="1.0.0",
)

origins = [o.strip() for o in settings.cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent: FinanceRAGAgent | None = None


def get_agent() -> FinanceRAGAgent:
    global _agent
    if _agent is None:
        _agent = FinanceRAGAgent()
    return _agent


class AskRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=4000)
    service_line: str | None = None


class AskResponse(BaseModel):
    answer: str
    citations: list[dict[str, Any]]
    confidence: float
    metrics: dict[str, Any]
    guardrails: list[str]
    refused: bool
    trace_id: str | None


class IndexRequest(BaseModel):
    paths: list[str] = Field(default_factory=lambda: ["data/corpus"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "finance-rag", "company": settings.company_name}


@app.post("/v1/ask", response_model=AskResponse)
def ask(payload: AskRequest) -> AskResponse:
    with track_request("ask") as meta:
        try:
            result = get_agent().ask(payload.query, service_line=payload.service_line)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ask_failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        meta["hit_rate"] = result.metrics.hit_rate
        meta["refused"] = result.refused
        return AskResponse(
            answer=result.answer,
            citations=[asdict(c) for c in result.citations],
            confidence=result.confidence,
            metrics=asdict(result.metrics),
            guardrails=result.guardrails,
            refused=result.refused,
            trace_id=result.trace_id,
        )


@app.post("/v1/index")
def index(payload: IndexRequest) -> dict[str, Any]:
    with track_request("index"):
        try:
            stats = index_corpus(payload.paths)
        except Exception as exc:  # noqa: BLE001
            logger.exception("index_failed", error=str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "indexed", **stats}


@app.post("/v1/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    from pathlib import Path

    upload_dir = Path("data/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / (file.filename or "upload.bin")
    content = await file.read()
    target.write_bytes(content)
    stats = index_corpus([str(target)])
    return {"status": "uploaded_and_indexed", "path": str(target), **stats}


@app.get("/metrics")
def prometheus_metrics() -> Response:
    if not settings.enable_prometheus:
        raise HTTPException(status_code=404, detail="Prometheus disabled")
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
