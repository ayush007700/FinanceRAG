"""Tracked background indexing jobs.

``/v1/index`` used to run the whole pipeline inside the request. On a real
corpus that means a timed-out connection with no way to see progress, know
whether it finished, or retry -- and the work continues invisibly after the
client has given up.

A job row makes the work observable and retryable. Execution is in-process via
FastAPI background tasks, which is correct for a single task and adequate for a
few: the durability limit is that a task killed mid-job leaves a row in
``running``. ``reap_stale_jobs`` marks those failed so they are visibly
retryable rather than silently stuck, and the row schema is already the shape a
SQS-backed worker would consume when volume justifies one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from finance_rag.config import get_settings
from finance_rag.db import connection
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class Job:
    job_id: int
    status: str
    org_id: str
    paths: list[str]
    documents: int = 0
    chunks: int = 0
    error: str | None = None


def create_job(paths: list[str], org_id: str | None = None, source_uri: str | None = None) -> int:
    org = org_id or get_settings().default_org_id
    with connection() as conn:
        job_id = conn.execute(
            text(
                """
                INSERT INTO index_jobs (org_id, status, paths, source_uri)
                VALUES (:org, 'queued', :paths, :source_uri)
                RETURNING job_id
                """
            ),
            {"org": org, "paths": list(paths), "source_uri": source_uri},
        ).scalar_one()
    logger.info("index_job_created", job_id=job_id, org_id=org, paths=len(paths))
    return int(job_id)


def _mark(job_id: int, status: str, **fields: Any) -> None:
    sets = ["status = :status"]
    params: dict[str, Any] = {"job_id": job_id, "status": status}
    if status == "running":
        sets.append("started_at = now()")
        sets.append("attempts = attempts + 1")
    if status in {"succeeded", "failed"}:
        sets.append("finished_at = now()")
    for key, value in fields.items():
        sets.append(f"{key} = :{key}")
        params[key] = value
    with connection() as conn:
        conn.execute(
            text(f"UPDATE index_jobs SET {', '.join(sets)} WHERE job_id = :job_id"), params
        )


def run_job(job_id: int) -> None:
    """Execute one indexing job. Never raises into the caller.

    Failure is recorded on the row: a background task that raises would lose the
    reason, leaving a job stuck in ``running`` with nothing to diagnose.
    """
    from finance_rag.pipeline import index_corpus

    with connection() as conn:
        row = conn.execute(
            text("SELECT paths, org_id FROM index_jobs WHERE job_id = :id"),
            {"id": job_id},
        ).mappings().first()
    if row is None:
        logger.warning("index_job_missing", job_id=job_id)
        return

    _mark(job_id, "running")
    try:
        stats = index_corpus(list(row["paths"]), org_id=row["org_id"])
        _mark(
            job_id,
            "succeeded",
            documents=int(stats.get("documents") or 0),
            chunks=int(stats.get("chunks") or 0),
            image_chunks=int(stats.get("image_chunks") or 0),
            entity_links=int(stats.get("entity_links") or 0),
        )
        logger.info("index_job_succeeded", job_id=job_id, **stats)
    except BaseException as exc:
        # BaseException, not Exception. A cancelled task raises
        # asyncio.CancelledError and a killed worker raises SystemExit -- both
        # derive from BaseException, so catching Exception let the row stay at
        # "running" forever while claiming work was in progress. A job table
        # that lies about its own state is worse than no job table.
        _mark(job_id, "failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        logger.exception("index_job_failed", job_id=job_id, error=str(exc))
        # Re-raise anything that is not a normal error so the process still
        # exits when it is being shut down.
        if not isinstance(exc, Exception):
            raise


def get_job(job_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            text("SELECT * FROM index_jobs WHERE job_id = :id"), {"id": job_id}
        ).mappings().first()
        return dict(row) if row else None


def list_jobs(limit: int = 20, org_id: str | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT job_id, org_id, status, paths, documents, chunks, attempts,
               error, created_at, started_at, finished_at
        FROM index_jobs
        {where}
        ORDER BY job_id DESC
        LIMIT :limit
    """.format(where="WHERE org_id = :org" if org_id else "")
    params: dict[str, Any] = {"limit": limit}
    if org_id:
        params["org"] = org_id
    with connection() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def reap_stale_jobs(older_than_minutes: int = 60) -> int:
    """Fail jobs stuck in ``running`` past a plausible runtime.

    A task killed mid-job leaves its row claiming to be running forever. Marking
    it failed is what makes it visibly retryable instead of quietly abandoned.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    with connection() as conn:
        result = conn.execute(
            text(
                """
                UPDATE index_jobs
                SET status = 'failed',
                    finished_at = now(),
                    error = COALESCE(error, 'abandoned: worker stopped before completion')
                WHERE status = 'running' AND started_at < :cutoff
                """
            ),
            {"cutoff": cutoff},
        )
        count = result.rowcount or 0
    if count:
        logger.warning("index_jobs_reaped", count=count, older_than_minutes=older_than_minutes)
    return count


def resolve_upload_paths(keys: list[str], workdir: Path) -> list[str]:
    """Stage stored objects locally so the parsers can read real paths."""
    from finance_rag.storage import build_object_store, read_into
    from finance_rag.storage.object_store import StoredObject

    store = build_object_store()
    workdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for key in keys:
        obj = StoredObject(uri="", key=key, size=0, checksum="")
        paths.append(str(read_into(store, obj, workdir)))
    return paths
