"""Rate limiter behaviour.

These exercise the per-process fallback deliberately: it is the path that runs
when Redis is absent, which is the case in CI and in local development, and a
limiter whose degraded path is untested is a limiter that fails open in exactly
the environment where nobody is watching.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from finance_rag.api import ratelimit
from finance_rag.config import get_settings


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Force the local backend and a clean counter table per test."""
    monkeypatch.setattr(ratelimit, "_redis_failed", True)
    monkeypatch.setattr(ratelimit, "_redis_client", None)
    monkeypatch.setattr(ratelimit, "_local", ratelimit._LocalWindows())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _limits(monkeypatch, *, ask=3, index=2, read=5, enabled=True):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("RATE_LIMIT_ASK", str(ask))
    monkeypatch.setenv("RATE_LIMIT_INDEX", str(index))
    monkeypatch.setenv("RATE_LIMIT_READ", str(read))
    monkeypatch.setenv("RATE_LIMIT_USE_REDIS", "false")
    get_settings.cache_clear()


def test_allows_up_to_the_limit(monkeypatch):
    _limits(monkeypatch, ask=3)
    for _ in range(3):
        ratelimit.enforce("k1", "default", frozenset({"ask"}))


def test_rejects_past_the_limit_with_retry_after(monkeypatch):
    _limits(monkeypatch, ask=2)
    ratelimit.enforce("k1", "default", frozenset({"ask"}))
    ratelimit.enforce("k1", "default", frozenset({"ask"}))

    with pytest.raises(HTTPException) as exc:
        ratelimit.enforce("k1", "default", frozenset({"ask"}))

    assert exc.value.status_code == 429
    # Without Retry-After a client can only guess, and guessing clients retry
    # immediately -- which is the behaviour the limit exists to prevent.
    assert int(exc.value.headers["Retry-After"]) >= 1
    assert exc.value.headers["X-RateLimit-Limit"] == "2"


def test_limits_are_per_credential(monkeypatch):
    """One key exhausting its budget must not lock out another tenant's key."""
    _limits(monkeypatch, ask=1)
    ratelimit.enforce("k1", "org-a", frozenset({"ask"}))
    with pytest.raises(HTTPException):
        ratelimit.enforce("k1", "org-a", frozenset({"ask"}))

    ratelimit.enforce("k2", "org-b", frozenset({"ask"}))


def test_limits_are_per_scope(monkeypatch):
    """Spending the ask budget must not block a read."""
    _limits(monkeypatch, ask=1, read=5)
    ratelimit.enforce("k1", "default", frozenset({"ask"}))
    with pytest.raises(HTTPException):
        ratelimit.enforce("k1", "default", frozenset({"ask"}))

    ratelimit.enforce("k1", "default", frozenset({"read"}))


def test_tightest_scope_wins(monkeypatch):
    """A route demanding several scopes gets the most restrictive of them."""
    _limits(monkeypatch, ask=30, index=2, read=120)
    both = frozenset({"ask", "index"})
    ratelimit.enforce("k1", "default", both)
    ratelimit.enforce("k1", "default", both)
    with pytest.raises(HTTPException):
        ratelimit.enforce("k1", "default", both)


def test_disabled_never_rejects(monkeypatch):
    _limits(monkeypatch, ask=1, enabled=False)
    for _ in range(10):
        ratelimit.enforce("k1", "default", frozenset({"ask"}))


def test_backend_failure_fails_open(monkeypatch):
    """A broken limiter must not become an outage.

    Refusing traffic because the counter store is unreachable turns a cost
    control into a availability incident, which is the wrong trade for a
    limit measured in tens per minute.
    """
    _limits(monkeypatch, ask=1)

    class _Broken:
        def pipeline(self):
            raise RuntimeError("redis is down")

    monkeypatch.setattr(ratelimit, "_redis_failed", False)
    monkeypatch.setattr(ratelimit, "_redis_client", _Broken())

    for _ in range(5):
        ratelimit.enforce("k1", "default", frozenset({"ask"}))


def test_window_rollover_resets_the_count(monkeypatch):
    """The next window starts fresh rather than carrying the previous count."""
    _limits(monkeypatch, ask=1)
    real_time = ratelimit.time.time
    base = real_time()

    monkeypatch.setattr(ratelimit.time, "time", lambda: base)
    ratelimit.enforce("k1", "default", frozenset({"ask"}))
    with pytest.raises(HTTPException):
        ratelimit.enforce("k1", "default", frozenset({"ask"}))

    monkeypatch.setattr(ratelimit.time, "time", lambda: base + 61)
    ratelimit.enforce("k1", "default", frozenset({"ask"}))
