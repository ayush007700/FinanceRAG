"""FastAPI application for Source Advisors FinanceRAG."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from finance_rag.agent import MultiAgentRAG
from finance_rag.config import get_settings
from finance_rag.logging_setup import configure_logging, get_logger
from finance_rag.monitoring import track_request
from finance_rag.observability import configure_langsmith
from finance_rag.observability import is_enabled as langfuse_enabled

configure_logging()
configure_langsmith()
logger = get_logger(__name__)
settings = get_settings()

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Reap jobs abandoned by a previous container before serving.

    A deploy or a kill leaves rows claiming to be running with nobody running
    them. Startup is exactly when the replacement container can say so, and a
    job table that lies about its own state is worse than no job table.
    """
    try:
        from finance_rag.pipeline.jobs import reap_stale_jobs

        reaped = await run_in_threadpool(reap_stale_jobs, settings.stale_job_minutes)
        if reaped:
            logger.warning("stale_jobs_reaped_on_startup", count=reaped)
    except Exception as exc:  # noqa: BLE001
        # Never block startup on housekeeping.
        logger.warning("startup_reap_failed", error=str(exc))
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Source Advisors FinanceRAG",
    description="LangGraph multimodal RAG over Postgres/pgvector with RRF hybrid retrieval, Redis semantic cache and LangSmith tracing",
    version="2.0.0",
)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
_wildcard = origins == ["*"]

if _wildcard and settings.app_env != "development":
    # A wildcard origin on a deployed API means any site can call it with the
    # caller's credentials. Loud, because the failure is silent otherwise.
    logger.warning(
        "cors_wildcard_in_deployment",
        reason="CORS_ORIGINS=* outside development; pin it to the UI origin",
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    # Browsers reject "*" combined with credentials outright, so the pair is
    # mutually exclusive: a wildcard origin forces credentials off rather than
    # producing a config the browser silently discards.
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent: MultiAgentRAG | None = None


def get_agent() -> MultiAgentRAG:
    global _agent
    if _agent is None:
        from finance_rag.memory import build_checkpointer

        _agent = MultiAgentRAG(checkpointer=build_checkpointer())
    return _agent


class AskRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=4000)
    service_line: str | None = None
    # Supplying the same thread_id across requests resumes the conversation,
    # which is what makes a follow-up like "and for 2023?" resolvable.
    thread_id: str | None = Field(
        default=None, description="Conversation id for multi-turn memory"
    )
    image_base64: str | None = Field(
        default=None, description="Optional image (base64) for multimodal ask"
    )
    image_mime: str = "image/png"
    # Answer as the corpus stood on this date. Tax guidance is superseded by
    # legislative cycle, so "what was the rule then" is a distinct question
    # from "what is the rule now".
    as_of: date | None = None


class AskResponse(BaseModel):
    answer: str
    citations: list[dict[str, Any]]
    confidence: float
    metrics: dict[str, Any]
    guardrails: list[str]
    refused: bool
    trace_id: str | None
    cache_hit: bool = False
    cache_layer: str | None = None


def citation_payload(citation) -> dict[str, Any]:
    """Serialise a citation for the API, dropping the full passage text.

    The full text exists on the dataclass for offline judging; sending it would
    multiply response size by roughly ten for no benefit to the client, which
    already renders the excerpt.
    """
    data = asdict(citation)
    data.pop("text", None)
    return data


class IndexRequest(BaseModel):
    paths: list[str] = Field(default_factory=lambda: ["data/corpus"])


@app.get("/health")
def health() -> dict[str, Any]:
    from finance_rag.cache import SemanticCache
    from finance_rag.db import healthcheck

    cache = SemanticCache()
    db_ok = healthcheck()
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "finance-rag",
        "company": settings.company_name,
        "database": db_ok,
        "cache_enabled": cache.enabled,
        "langsmith": bool(settings.langsmith_tracing and settings.langsmith_api_key),
        # Reports whether tracing is actually reachable, not merely configured.
        "langfuse": langfuse_enabled(),
        "multimodal": settings.multimodal_enabled,
    }


def tenant(x_org_id: str | None = Header(default=None)) -> str:
    """Resolve the calling tenant.

    A header is the placeholder for real authentication: when auth lands this
    becomes a claim from the verified token. It is isolated here so that swap
    touches one function rather than every endpoint.
    """
    settings_ = get_settings()
    if not settings_.enforce_tenancy:
        return settings_.default_org_id
    return (x_org_id or settings_.default_org_id).strip() or settings_.default_org_id


@app.post("/v1/ask", response_model=AskResponse)
async def ask(payload: AskRequest, org_id: str = Depends(tenant)) -> AskResponse:
    """Answer one question.

    The agent is synchronous and makes several blocking network calls, so it
    runs in a worker thread. Awaiting it directly on the event loop would stall
    every other in-flight request for the duration.
    """
    return await run_in_threadpool(_ask_sync, payload, org_id)


def _ask_sync(payload: AskRequest, org_id: str) -> AskResponse:
    with track_request("ask") as meta:
        image_bytes = None
        if payload.image_base64:
            try:
                image_bytes = base64.b64decode(payload.image_base64)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid image_base64: {exc}") from exc
        try:
            result = get_agent().ask(
                payload.query,
                thread_id=payload.thread_id,
                service_line=payload.service_line,
                image_bytes=image_bytes,
                image_mime=payload.image_mime,
                org_id=org_id,
                as_of=payload.as_of,
            )
        except Exception as tip:
            logger.exception("ask_failed", error=str(tip))
            raise HTTPException(status_code=500, detail=str(tip)) from tip
        # hit_rate is label-backed and therefore unavailable online. Top cosine
        # is the real absolute signal, so that is what drives the retrieval gauge.
        meta["top_cosine"] = result.metrics.top_cosine
        meta["refused"] = result.refused
        meta["hallucinated_citations"] = len(result.metrics.hallucinated_citations)
        return AskResponse(
            answer=result.answer,
            citations=[citation_payload(c) for c in result.citations],
            confidence=result.confidence,
            metrics=asdict(result.metrics),
            guardrails=result.guardrails,
            refused=result.refused,
            trace_id=result.trace_id,
            cache_hit=result.cache_hit,
            cache_layer=result.cache_layer,
        )


@app.post("/v1/ask/multipart", response_model=AskResponse)
async def ask_multipart(
    query: str = Form(...),
    service_line: str | None = Form(None),
    image: UploadFile | None = File(None),
    org_id: str = Depends(tenant),
) -> AskResponse:
    image_bytes = await image.read() if image is not None else None
    mime = image.content_type if image and image.content_type else "image/png"
    payload = AskRequest(
        query=query,
        service_line=service_line,
        image_base64=base64.b64encode(image_bytes).decode("ascii") if image_bytes else None,
        image_mime=mime,
    )
    # Same threadpool hop as /v1/ask: the agent blocks, and this endpoint is a
    # coroutine, so calling it directly would stall the event loop.
    return await run_in_threadpool(_ask_sync, payload, org_id)


@app.post("/v1/index", status_code=202)
def index(
    payload: IndexRequest,
    background: BackgroundTasks,
    org_id: str = Depends(tenant),
) -> dict[str, Any]:
    """Queue an indexing job.

    Returns 202 with a job id rather than running inline: on a real corpus the
    pipeline outlives any sane request timeout, and a client that gives up has
    no way to learn whether the work finished or how it failed.
    """
    from finance_rag.pipeline.launcher import dispatch_index_job

    d = dispatch_index_job(payload.paths, org_id=org_id)
    if d.error:
        raise HTTPException(status_code=503, detail=d.error)
    return {
        "status": "queued",
        "job_id": d.job_id,
        "runner": d.runner,
        "task_arn": d.task_arn,
        "poll": f"/v1/jobs/{d.job_id}",
    }


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: int, org_id: str = Depends(tenant)) -> dict[str, Any]:
    from finance_rag.pipeline.jobs import get_job

    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # 404 rather than 403: a job id must not confirm another tenant's work exists.
    if get_settings().enforce_tenancy and job.get("org_id") != org_id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/v1/jobs")
def jobs(limit: int = 20, org_id: str = Depends(tenant)) -> dict[str, Any]:
    from finance_rag.pipeline.jobs import list_jobs

    return {"jobs": list_jobs(limit=min(limit, 100), org_id=org_id)}


@app.post("/v1/upload", status_code=202)
async def upload(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    org_id: str = Depends(tenant),
) -> dict[str, Any]:
    """Store an uploaded document durably, then queue indexing.

    The bytes reach object storage before anything else. Writing to the task
    filesystem meant the document vanished on redeploy and was invisible to
    every other task, so retrieval succeeded only intermittently -- which is
    worse than failing outright, because it looks like a ranking problem.
    """
    from finance_rag.pipeline.launcher import dispatch_index_job
    from finance_rag.storage import build_object_store, content_key

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty upload")

    store = build_object_store()
    key = content_key(org_id, file.filename or "upload.bin", content)
    stored = await run_in_threadpool(store.put, key, content)

    # The object key is the path: the indexing task stages it from storage,
    # which is why the upload had to become durable before this could work.
    d = await run_in_threadpool(dispatch_index_job, [stored.key], org_id)
    if d.error:
        raise HTTPException(status_code=503, detail=d.error)
    return {
        "status": "queued",
        "job_id": d.job_id,
        "runner": d.runner,
        "uri": stored.uri,
        "checksum": stored.checksum,
        "poll": f"/v1/jobs/{d.job_id}",
    }


def _sse(event: dict[str, Any]) -> str:
    """Frame one server-sent event. The blank line terminates the frame."""
    import json as _json

    return f"data: {_json.dumps(event)}\n\n"


@app.post("/v1/ask/stream")
async def ask_stream(payload: AskRequest, org_id: str = Depends(tenant)):
    """Answer with server-sent events.

    The agent is not token-streaming: it routes, retrieves, verifies and only
    then produces prose, and a partially-written answer must not reach the user
    before the Critic has seen it. What streams instead is *progress* -- which
    role is working -- because the honest problem with a 5s answer is not that
    it lacks tokens, it is that the page looks broken.

    Stages are emitted as they complete, then the verified answer once.
    """
    import asyncio

    async def events():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(stage: str, detail: str = "") -> None:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "stage", "stage": stage, "detail": detail}
            )

        def work() -> None:
            try:
                emit("routing")
                result = get_agent().ask(
                    payload.query,
                    thread_id=payload.thread_id,
                    service_line=payload.service_line,
                    org_id=org_id,
                    as_of=payload.as_of,
                    on_stage=emit,
                )
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "type": "answer",
                        "answer": result.answer,
                        "citations": [citation_payload(c) for c in result.citations],
                        "refused": result.refused,
                        "confidence": result.confidence,
                        "trace_id": result.trace_id,
                    },
                )
            except Exception as exc:
                logger.exception("stream_failed", error=str(exc))
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "error", "detail": str(exc)}
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.create_task(asyncio.to_thread(work))
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _sse(event)
            yield _sse({"type": "done"})
        finally:
            # A client that disconnects mid-answer must not leave the worker
            # thread orphaned holding a database connection.
            task.cancel()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/audit")
def audit(limit: int = 20, thread_id: str | None = None) -> dict[str, Any]:
    """Recent interactions from the append-only audit trail."""
    from finance_rag.memory import recent_audits

    return {"records": recent_audits(limit=min(limit, 200), thread_id=thread_id)}


@app.get("/v1/eval/runs")
def eval_runs(limit: int = 20) -> dict[str, Any]:
    """Evaluation run history: which config produced which numbers."""
    from finance_rag.evaluation import list_runs

    return {"runs": list_runs(limit=min(limit, 100))}


@app.get("/metrics")
def prometheus_metrics() -> Response:
    if not settings.enable_prometheus:
        raise HTTPException(status_code=404, detail="Prometheus disabled")
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
