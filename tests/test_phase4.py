"""Tenancy, durable storage, async jobs and streaming.

The four gaps that blocked real deployment. Integration tests skip without a
database; everything else runs anywhere.
"""

from __future__ import annotations

from datetime import date

import pytest

from finance_rag.config import get_settings
from finance_rag.storage import LocalObjectStore, content_key


def _database_available() -> bool:
    try:
        from finance_rag.db import healthcheck

        return healthcheck()
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(not _database_available(), reason="Postgres not reachable")

# CI runs a real database but deliberately no model credentials, so a test
# needing embeddings must guard on both. Guarding only on the database made this
# fail in CI while passing locally.
requires_openai = pytest.mark.skipif(
    not get_settings().openai_api_key, reason="OPENAI_API_KEY not set"
)


@pytest.fixture(autouse=True)
def _reset():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# object storage
# --------------------------------------------------------------------------


def test_upload_keys_are_content_addressed(tmp_path):
    """Re-uploading identical bytes must not create a second copy to index twice."""
    data = b"the four-part test"
    assert content_key("acme", "a.pdf", data) == content_key("acme", "a.pdf", data)
    assert content_key("acme", "a.pdf", data) != content_key("acme", "a.pdf", b"different")


def test_upload_keys_are_namespaced_by_tenant():
    data = b"x"
    assert content_key("acme", "a.pdf", data).startswith("acme/")
    assert content_key("globex", "a.pdf", data).startswith("globex/")


def test_local_store_roundtrip(tmp_path):
    store = LocalObjectStore(tmp_path)
    obj = store.put("acme/uploads/abc/report.pdf", b"hello")
    assert store.exists(obj.key)
    assert store.get(obj.key) == b"hello"
    assert obj.size == 5
    assert obj.is_remote is False


def test_traversal_key_cannot_escape_the_store(tmp_path):
    """A key derived from a filename must not write outside the root."""
    store = LocalObjectStore(tmp_path / "root")
    with pytest.raises(ValueError, match="escapes storage root"):
        store.put("../../etc/evil", b"x")


def test_filenames_with_separators_are_flattened():
    key = content_key("acme", "../../etc/passwd", b"x")
    assert ".." not in key.split("/")[-1]


def test_store_selection_falls_back_to_local_without_a_bucket(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "")
    get_settings.cache_clear()
    from finance_rag.storage import build_object_store

    assert isinstance(build_object_store(), LocalObjectStore)


# --------------------------------------------------------------------------
# tenancy
# --------------------------------------------------------------------------


def test_scope_predicate_always_filters_org():
    """Cross-tenant leakage is the one retrieval failure that cannot be undone,
    so org_id is an equality filter and never a soft ranking signal."""
    from finance_rag.store.pgvector_store import _scope_predicates

    assert "org_id = :org_id" in _scope_predicates(None)


def test_scope_predicate_adds_an_effective_window_when_dated():
    from finance_rag.store.pgvector_store import _scope_predicates

    sql = _scope_predicates(date(2026, 1, 1), alias="c")
    assert "c.effective_date <= :as_of" in sql
    assert "c.superseded_date > :as_of" in sql
    assert "c.org_id = :org_id" in sql


def test_undated_documents_remain_retrievable():
    """A corpus without dates must not vanish when effective dating is on."""
    from finance_rag.store.pgvector_store import _scope_predicates

    sql = _scope_predicates(date(2026, 1, 1))
    assert "effective_date IS NULL" in sql
    assert "superseded_date IS NULL" in sql


def test_tenant_header_resolves(monkeypatch):
    from finance_rag.api.app import tenant

    monkeypatch.setenv("ENFORCE_TENANCY", "true")
    get_settings.cache_clear()
    assert tenant(x_org_id="acme") == "acme"
    assert tenant(x_org_id=None) == get_settings().default_org_id
    assert tenant(x_org_id="   ") == get_settings().default_org_id


def test_tenancy_can_be_disabled_for_single_tenant_deployments(monkeypatch):
    from finance_rag.api.app import tenant

    monkeypatch.setenv("ENFORCE_TENANCY", "false")
    get_settings.cache_clear()
    assert tenant(x_org_id="acme") == get_settings().default_org_id


@requires_db
@requires_openai
def test_retrieval_is_isolated_between_tenants():
    """The corpus is indexed under 'default'; another org must see nothing."""
    from finance_rag.retrieval import HybridRetriever
    from finance_rag.store import PgVectorStore

    store = PgVectorStore()
    vec = HybridRetriever().embedder.embed_query("four-part test")
    mine = store.hybrid_search_rrf(embedding=vec, query_text="four-part test",
                                   top_k=5, org_id="default")
    theirs = store.hybrid_search_rrf(embedding=vec, query_text="four-part test",
                                     top_k=5, org_id="someone-else")
    assert mine, "default org should retrieve the indexed corpus"
    assert theirs == []


# --------------------------------------------------------------------------
# indexing jobs
# --------------------------------------------------------------------------


@pytest.fixture
def cleanup_jobs():
    ids: list[int] = []
    yield ids
    if ids and _database_available():
        from sqlalchemy import text

        from finance_rag.db import connection

        with connection() as conn:
            conn.execute(text("DELETE FROM index_jobs WHERE job_id = ANY(:ids)"), {"ids": ids})


@requires_db
def test_job_is_created_queued(cleanup_jobs):
    from finance_rag.pipeline.jobs import create_job, get_job

    job_id = create_job(["data/corpus"], org_id="acme")
    cleanup_jobs.append(job_id)
    job = get_job(job_id)
    assert job["status"] == "queued"
    assert job["org_id"] == "acme"
    assert job["attempts"] == 0


@requires_db
def test_failing_job_records_the_reason_rather_than_raising(cleanup_jobs):
    """A background task that raises loses the reason and strands the row."""
    from finance_rag.pipeline.jobs import create_job, get_job, run_job

    job_id = create_job(["/nonexistent/path/definitely"], org_id="acme")
    cleanup_jobs.append(job_id)
    run_job(job_id)  # must not raise

    job = get_job(job_id)
    assert job["status"] == "failed"
    assert job["error"]
    assert job["attempts"] == 1
    assert job["finished_at"] is not None


@requires_db
def test_stale_running_jobs_are_reaped(cleanup_jobs):
    """A killed task leaves a row claiming to run forever; reaping makes it
    visibly retryable instead of quietly abandoned."""
    from sqlalchemy import text

    from finance_rag.db import connection
    from finance_rag.pipeline.jobs import create_job, get_job, reap_stale_jobs

    job_id = create_job(["x"], org_id="acme")
    cleanup_jobs.append(job_id)
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE index_jobs SET status='running', "
                "started_at = now() - interval '3 hours' WHERE job_id = :id"
            ),
            {"id": job_id},
        )

    assert reap_stale_jobs(older_than_minutes=60) >= 1
    job = get_job(job_id)
    assert job["status"] == "failed"
    assert "abandoned" in job["error"]


@requires_db
def test_jobs_are_listed_per_tenant(cleanup_jobs):
    from finance_rag.pipeline.jobs import create_job, list_jobs

    mine = create_job(["a"], org_id="tenant-a")
    theirs = create_job(["b"], org_id="tenant-b")
    cleanup_jobs.extend([mine, theirs])

    ids = {j["job_id"] for j in list_jobs(limit=50, org_id="tenant-a")}
    assert mine in ids
    assert theirs not in ids


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


def test_sse_frames_end_with_a_blank_line():
    """The blank line terminates the frame; without it a client never dispatches."""
    from finance_rag.api.app import _sse

    frame = _sse({"type": "stage", "stage": "retrieving"})
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert '"stage": "retrieving"' in frame


def test_stage_emission_never_breaks_the_request():
    """Progress reporting is cosmetic; it must not be able to fail an answer."""
    from finance_rag.agent.orchestrator import MultiAgentRAG

    agent = MultiAgentRAG.__new__(MultiAgentRAG)
    agent._on_stage = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("client gone"))
    agent._emit("retrieving")  # must not raise


def test_stage_emission_is_a_noop_without_a_listener():
    from finance_rag.agent.orchestrator import MultiAgentRAG

    agent = MultiAgentRAG.__new__(MultiAgentRAG)
    agent._on_stage = None
    agent._emit("retrieving")


def test_index_and_upload_return_202_not_a_blocking_result():
    """Both must hand back a job id rather than running the pipeline inline."""
    import importlib

    app_module = importlib.import_module("finance_rag.api.app")
    routes = {r.path: r for r in app_module.app.routes if hasattr(r, "path")}
    assert routes["/v1/index"].status_code == 202
    assert routes["/v1/upload"].status_code == 202
    assert "/v1/jobs/{job_id}" in routes


def test_ask_runs_off_the_event_loop():
    """A coroutine calling the blocking agent directly stalls every other request."""
    import importlib
    import inspect

    # finance_rag.api exports the FastAPI instance as `app`, which shadows the
    # submodule of the same name, so import it explicitly.
    app_module = importlib.import_module("finance_rag.api.app")
    assert "run_in_threadpool" in inspect.getsource(app_module.ask)
    assert "run_in_threadpool" in inspect.getsource(app_module.ask_multipart)


def test_upload_path_is_not_the_container_filesystem():
    """Regression: uploads went to data/uploads and vanished on redeploy."""
    import importlib
    import inspect

    app_module = importlib.import_module("finance_rag.api.app")
    src = inspect.getsource(app_module.upload)
    assert "build_object_store" in src
    assert 'Path("data/uploads")' not in src
