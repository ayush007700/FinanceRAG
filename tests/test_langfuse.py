"""Self-hosted Langfuse integration.

The behaviour that matters is that it never breaks anything. Observability which
can take down the service it observes is a liability, so every path here
degrades to no tracing rather than to no answers.

No network: the client is stubbed or disabled throughout.
"""

from __future__ import annotations

import pytest

from finance_rag.config import get_settings
from finance_rag.observability import langfuse_setup


@pytest.fixture(autouse=True)
def _reset():
    get_settings.cache_clear()
    langfuse_setup._client.cache_clear()
    yield
    get_settings.cache_clear()
    langfuse_setup._client.cache_clear()


def _enable(monkeypatch, **extra):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", extra.pop("public", "pk-lf-test"))
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", extra.pop("secret", "sk-lf-test"))
    monkeypatch.setenv("LANGFUSE_HOST", extra.pop("host", "http://localhost:3001"))
    get_settings.cache_clear()
    langfuse_setup._client.cache_clear()


class _FakeClient:
    def __init__(self, raises: Exception | None = None, auth: bool = True):
        self.raises = raises
        self.auth = auth
        self.scores: list[dict] = []
        self.flushed = 0

    def create_score(self, **kwargs):
        if self.raises:
            raise self.raises
        self.scores.append(kwargs)

    def flush(self):
        self.flushed += 1

    def auth_check(self):
        if self.raises:
            raise self.raises
        return self.auth


# --------------------------------------------------------------------------
# configuration gating
# --------------------------------------------------------------------------


def test_disabled_by_default():
    """Off unless explicitly switched on: it needs a server to talk to."""
    assert get_settings().langfuse_enabled is False
    assert langfuse_setup.is_enabled() is False


def test_enabled_without_keys_stays_disabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    get_settings.cache_clear()
    langfuse_setup._client.cache_clear()
    assert langfuse_setup.is_enabled() is False


def test_no_handler_when_disabled():
    assert langfuse_setup.callback_handler() is None


def test_client_construction_failure_degrades_to_disabled(monkeypatch):
    """An unreachable or broken client must not raise into the request path."""
    _enable(monkeypatch)
    import langfuse

    monkeypatch.setattr(
        langfuse, "Langfuse", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert langfuse_setup._client() is None
    assert langfuse_setup.is_enabled() is False


# --------------------------------------------------------------------------
# eval score publishing
# --------------------------------------------------------------------------


def test_eval_metrics_are_published_as_scores(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(langfuse_setup, "_client", lambda: fake)

    langfuse_setup.record_eval_run(
        42, {"hit_rate": 0.9, "mrr": 0.8, "n_cases": 39}, label="ci-abc1234"
    )
    names = {s["name"] for s in fake.scores}
    assert "eval.hit_rate" in names
    assert "eval.mrr" in names
    assert fake.flushed == 1
    assert all(s["metadata"]["eval_run_id"] == 42 for s in fake.scores)


def test_non_numeric_summary_fields_are_skipped(monkeypatch):
    """by_service_line is a dict; a score must be a number."""
    fake = _FakeClient()
    monkeypatch.setattr(langfuse_setup, "_client", lambda: fake)

    langfuse_setup.record_eval_run(
        1, {"hit_rate": 0.9, "by_service_line": {"R&D": {}}, "label": "x"}
    )
    assert {s["name"] for s in fake.scores} == {"eval.hit_rate"}


def test_booleans_are_not_published_as_metrics(monkeypatch):
    """A bool is an int in Python; publishing True as 1.0 misreports a metric."""
    fake = _FakeClient()
    monkeypatch.setattr(langfuse_setup, "_client", lambda: fake)

    langfuse_setup.record_eval_run(1, {"hit_rate": 0.5, "passed": True})
    assert {s["name"] for s in fake.scores} == {"eval.hit_rate"}


def test_publishing_failure_never_raises(monkeypatch):
    """An observability failure must not fail the eval."""
    fake = _FakeClient(raises=RuntimeError("langfuse down"))
    monkeypatch.setattr(langfuse_setup, "_client", lambda: fake)
    langfuse_setup.record_eval_run(1, {"hit_rate": 0.9})  # must not raise


def test_publishing_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(langfuse_setup, "_client", lambda: None)
    langfuse_setup.record_eval_run(1, {"hit_rate": 0.9})  # must not raise


def test_empty_summary_is_ignored(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(langfuse_setup, "_client", lambda: fake)
    langfuse_setup.record_eval_run(1, {})
    assert fake.scores == []


# --------------------------------------------------------------------------
# connection check and flush
# --------------------------------------------------------------------------


def test_connection_check_reports_reachability(monkeypatch):
    monkeypatch.setattr(langfuse_setup, "_client", lambda: _FakeClient(auth=True))
    assert langfuse_setup.check_connection() is True


def test_connection_check_failure_is_false_not_an_exception(monkeypatch):
    monkeypatch.setattr(
        langfuse_setup, "_client", lambda: _FakeClient(raises=RuntimeError("no route"))
    )
    assert langfuse_setup.check_connection() is False


def test_connection_check_when_disabled():
    assert langfuse_setup.check_connection() is False


def test_flush_is_safe_when_disabled(monkeypatch):
    monkeypatch.setattr(langfuse_setup, "_client", lambda: None)
    langfuse_setup.flush()  # must not raise


def test_flush_drains_buffered_events(monkeypatch):
    """Langfuse batches, so a short-lived process must flush or lose the trace."""
    fake = _FakeClient()
    monkeypatch.setattr(langfuse_setup, "_client", lambda: fake)
    langfuse_setup.flush()
    assert fake.flushed == 1


# --------------------------------------------------------------------------
# coexistence with LangSmith
# --------------------------------------------------------------------------


def test_langfuse_and_langsmith_are_independent(monkeypatch):
    """They are not redundant: one is hosted convenience, one can face production."""
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.langfuse_enabled is True
    assert settings.langsmith_tracing is False
