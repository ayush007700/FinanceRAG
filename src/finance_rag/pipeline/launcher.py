"""Dispatch indexing work to a container that can actually finish it.

``/v1/index`` originally ran the pipeline as a FastAPI background task, inside
the web container. That could not work, for two independent reasons:

*Memory.* The API service runs on 0.5 vCPU / 1 GiB because serving requests is
IO-bound on model APIs. Parsing a 113-page PDF is not: pdfplumber holds the
whole page model, and the same corpus needed 8 GiB to index. In production the
task was SIGKILLed with exit 137 every time, while the endpoint had already
returned 202 -- an endpoint that always reports success and never delivers.

*Lifecycle.* A background task shares the web container's lifetime, so a deploy,
a scale-in or a cancelled request takes the job with it. That happened too.

So indexing runs as its own ECS task with its own sizing and its own exit code.
The job row is updated by that task, not by the caller, so the row reflects what
actually happened rather than what was dispatched.

``INDEX_RUNNER=inline`` keeps the old in-process path for local development,
where there is no ECS and no memory limit worth worrying about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.pipeline.jobs import create_job

logger = get_logger(__name__)


@dataclass
class Dispatch:
    job_id: int
    runner: str
    task_arn: str | None = None
    error: str | None = None


def _subnets() -> list[str]:
    return [s.strip() for s in get_settings().index_task_subnets.split(",") if s.strip()]


def _security_groups() -> list[str]:
    return [
        s.strip() for s in get_settings().index_task_security_groups.split(",") if s.strip()
    ]


def _run_ecs_task(job_id: int) -> str:
    """Launch the indexing task. Returns its ARN.

    The task runs ``finance-rag run-job <id>``, so it marks the row running,
    succeeded or failed itself. The API never claims an outcome it did not
    observe.
    """
    import boto3

    settings = get_settings()
    client = boto3.client("ecs", region_name=settings.aws_region)

    response = client.run_task(
        cluster=settings.ecs_cluster,
        taskDefinition=settings.index_task_definition,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": _subnets(),
                "securityGroups": _security_groups(),
                # Without a NAT gateway the task needs a public IP to reach the
                # model APIs at all.
                "assignPublicIp": "ENABLED"
                if settings.index_task_assign_public_ip
                else "DISABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": settings.index_task_container,
                    "command": ["finance-rag", "run-job", str(job_id)],
                }
            ]
        },
        # Traceable back to the row it services.
        startedBy=f"index-job-{job_id}"[:36],
    )

    failures = response.get("failures") or []
    if failures:
        raise RuntimeError(f"ECS refused the task: {json.dumps(failures)}")

    tasks = response.get("tasks") or []
    if not tasks:
        raise RuntimeError("ECS returned no task")
    return str(tasks[0]["taskArn"])


def dispatch_index_job(paths: list[str], org_id: str | None = None) -> Dispatch:
    """Create a job row and start the work that will fulfil it."""
    settings = get_settings()
    job_id = create_job(paths, org_id=org_id)
    runner = settings.index_runner

    if runner == "ecs":
        try:
            task_arn = _run_ecs_task(job_id)
            logger.info("index_job_dispatched", job_id=job_id, task_arn=task_arn)
            return Dispatch(job_id=job_id, runner="ecs", task_arn=task_arn)
        except Exception as exc:  # noqa: BLE001
            # Mark the row failed rather than leaving it queued forever: a job
            # nobody is running must not look pending.
            from finance_rag.pipeline.jobs import _mark

            message = f"dispatch failed: {exc}"
            _mark(job_id, "failed", error=message[:2000])
            logger.error("index_job_dispatch_failed", job_id=job_id, error=str(exc))
            return Dispatch(job_id=job_id, runner="ecs", error=message)

    # Local development: run in a thread. Acceptable only because there is no
    # container to be replaced and no memory ceiling to hit.
    import threading

    from finance_rag.pipeline.jobs import run_job

    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    logger.info("index_job_started_inline", job_id=job_id)
    return Dispatch(job_id=job_id, runner="inline")
