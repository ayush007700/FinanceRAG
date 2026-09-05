"""Indexing dispatch and job-state honesty.

Three defects observed in production, pinned here:

1. ``/v1/index`` ran the pipeline inside the API container, which is sized for
   IO-bound request serving and was SIGKILLed with exit 137 every time -- while
   the endpoint had already returned 202.
2. There was no task definition for indexing, so CPU and memory were overridden
   by hand at call time.
3. ``run_job`` caught ``Exception``, so a cancelled or killed worker left the
   row claiming to be running forever.
"""

from __future__ import annotations

import pytest

from finance_rag.config import get_settings


@pytest.fixture(autouse=True)
def _reset():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# runner selection
# --------------------------------------------------------------------------


def test_inline_is_the_local_default():
    assert get_settings().index_runner == "inline"


def test_invalid_runner_is_rejected_at_startup(monkeypatch):
    from finance_rag.config import Settings

    monkeypatch.setenv("INDEX_RUNNER", "kubernetes")
    with pytest.raises(ValueError, match="INDEX_RUNNER"):
        Settings()


def test_deployment_config_selects_ecs(monkeypatch):
    monkeypatch.setenv("INDEX_RUNNER", "ecs")
    get_settings.cache_clear()
    assert get_settings().index_runner == "ecs"


# --------------------------------------------------------------------------
# ECS dispatch
# --------------------------------------------------------------------------


class _FakeECS:
    def __init__(self, failures=None, tasks=None, raises=None):
        self.failures = failures or []
        self.tasks = tasks if tasks is not None else [{"taskArn": "arn:aws:ecs:task/abc"}]
        self.raises = raises
        self.calls: list[dict] = []

    def run_task(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return {"failures": self.failures, "tasks": self.tasks}


def _dispatch_with(monkeypatch, fake, job_id=7):
    monkeypatch.setenv("INDEX_RUNNER", "ecs")
    monkeypatch.setenv("ECS_CLUSTER", "test-cluster")
    monkeypatch.setenv("INDEX_TASK_DEFINITION", "test-index")
    monkeypatch.setenv("INDEX_TASK_SUBNETS", "subnet-a,subnet-b")
    monkeypatch.setenv("INDEX_TASK_SECURITY_GROUPS", "sg-1")
    get_settings.cache_clear()

    import boto3

    from finance_rag.pipeline import launcher

    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    monkeypatch.setattr(launcher, "create_job", lambda *a, **k: job_id)
    marks: list[tuple] = []
    monkeypatch.setattr(
        "finance_rag.pipeline.jobs._mark",
        lambda jid, status, **f: marks.append((jid, status, f)),
    )
    return launcher, marks


def test_dispatch_launches_a_task_running_the_job_id(monkeypatch):
    """The task updates the row itself, so the row records what happened."""
    fake = _FakeECS()
    launcher, _ = _dispatch_with(monkeypatch, fake)

    d = launcher.dispatch_index_job(["data/corpus"], org_id="acme")
    assert d.runner == "ecs"
    assert d.task_arn == "arn:aws:ecs:task/abc"
    assert d.error is None

    call = fake.calls[0]
    assert call["cluster"] == "test-cluster"
    assert call["taskDefinition"] == "test-index"
    override = call["overrides"]["containerOverrides"][0]
    assert override["command"] == ["finance-rag", "run-job", "7"]


def test_dispatch_passes_network_configuration(monkeypatch):
    fake = _FakeECS()
    launcher, _ = _dispatch_with(monkeypatch, fake)
    launcher.dispatch_index_job(["data/corpus"])

    net = fake.calls[0]["networkConfiguration"]["awsvpcConfiguration"]
    assert net["subnets"] == ["subnet-a", "subnet-b"]
    assert net["securityGroups"] == ["sg-1"]
    # Without NAT the task needs a public IP to reach the model APIs at all.
    assert net["assignPublicIp"] == "ENABLED"


def test_ecs_failure_marks_the_row_failed(monkeypatch):
    """A job nobody is running must not sit at queued forever."""
    fake = _FakeECS(raises=RuntimeError("no capacity"))
    launcher, marks = _dispatch_with(monkeypatch, fake)

    d = launcher.dispatch_index_job(["data/corpus"])
    assert d.error and "no capacity" in d.error
    assert marks and marks[-1][1] == "failed"


def test_ecs_rejection_is_surfaced_not_swallowed(monkeypatch):
    fake = _FakeECS(failures=[{"reason": "RESOURCE:MEMORY"}], tasks=[])
    launcher, marks = _dispatch_with(monkeypatch, fake)

    d = launcher.dispatch_index_job(["data/corpus"])
    assert d.error and "RESOURCE:MEMORY" in d.error
    assert marks[-1][1] == "failed"


def test_empty_task_list_is_an_error(monkeypatch):
    fake = _FakeECS(tasks=[])
    launcher, marks = _dispatch_with(monkeypatch, fake)
    assert launcher.dispatch_index_job(["x"]).error
    assert marks[-1][1] == "failed"


# --------------------------------------------------------------------------
# job-state honesty
# --------------------------------------------------------------------------


def test_run_job_records_cancellation_rather_than_stranding_the_row(monkeypatch):
    """Regression: two production jobs sat at "running" for hours.

    asyncio.CancelledError and SystemExit derive from BaseException, so an
    ``except Exception`` handler never marked them failed.
    """
    from finance_rag.pipeline import jobs

    marks: list[tuple] = []
    monkeypatch.setattr(jobs, "_mark", lambda jid, status, **f: marks.append((status, f)))

    class _Conn:
        def execute(self, *a, **k):
            return self

        def mappings(self):
            return self

        def first(self):
            return {"paths": ["x"], "org_id": "acme"}

    from contextlib import contextmanager

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(jobs, "connection", _conn)
    monkeypatch.setattr(
        "finance_rag.pipeline.index_corpus",
        lambda *a, **k: (_ for _ in ()).throw(BaseException("worker killed")),
    )

    with pytest.raises(BaseException, match="worker killed"):
        jobs.run_job(1)

    statuses = [m[0] for m in marks]
    assert "running" in statuses
    assert "failed" in statuses  # the row is not left claiming to run


def test_ordinary_errors_still_fail_the_row_without_re_raising(monkeypatch):
    from finance_rag.pipeline import jobs

    marks: list[tuple] = []
    monkeypatch.setattr(jobs, "_mark", lambda jid, status, **f: marks.append((status, f)))

    class _Conn:
        def execute(self, *a, **k):
            return self

        def mappings(self):
            return self

        def first(self):
            return {"paths": ["x"], "org_id": "acme"}

    from contextlib import contextmanager

    @contextmanager
    def _conn():
        yield _Conn()

    monkeypatch.setattr(jobs, "connection", _conn)
    monkeypatch.setattr(
        "finance_rag.pipeline.index_corpus",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad corpus")),
    )

    jobs.run_job(1)  # must not raise
    assert marks[-1][0] == "failed"
    assert "bad corpus" in marks[-1][1]["error"]


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------


def test_index_endpoint_dispatches_instead_of_running_in_process():
    """Regression: the endpoint returned 202 and then died with exit 137."""
    import importlib
    import inspect

    app_module = importlib.import_module("finance_rag.api.app")
    src = inspect.getsource(app_module.index)
    assert "dispatch_index_job" in src
    assert "background.add_task" not in src


def test_upload_endpoint_dispatches_too():
    import importlib
    import inspect

    app_module = importlib.import_module("finance_rag.api.app")
    src = inspect.getsource(app_module.upload)
    assert "dispatch_index_job" in src
    assert "background.add_task" not in src


def test_startup_reaps_abandoned_jobs():
    import importlib
    import inspect

    app_module = importlib.import_module("finance_rag.api.app")
    assert "reap_stale_jobs" in inspect.getsource(app_module.lifespan)


def test_cli_exposes_the_subcommands_the_task_runs():
    import importlib
    import inspect

    cli = importlib.import_module("finance_rag.cli")
    src = inspect.getsource(cli.main)
    assert '"run-job"' in src
    assert '"reap-jobs"' in src
