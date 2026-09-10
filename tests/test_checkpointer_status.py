"""Checkpointer status reporting.

The bug these guard against: multi-turn memory is on by default and advertised
in the README, but ``build_checkpointer`` degrades to None on any failure. An
undeclared dependency kept it off in every deployed image, and nothing outside a
single log line said so.
"""

from __future__ import annotations

import pytest

from finance_rag.config import get_settings
from finance_rag.memory import threads


@pytest.fixture(autouse=True)
def _reset():
    threads._last_error = None
    get_settings.cache_clear()
    yield
    threads._last_error = None
    get_settings.cache_clear()


def test_disabled_is_reported_as_a_choice(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_ENABLED", "false")
    get_settings.cache_clear()

    assert threads.checkpointer_status()["state"] == "disabled"


def test_ready_when_nothing_has_failed(monkeypatch):
    monkeypatch.setenv("CONVERSATION_MEMORY_ENABLED", "true")
    get_settings.cache_clear()

    assert threads.checkpointer_status()["state"] == "ready"


def test_failure_is_reported_as_a_fault_not_a_setting(monkeypatch):
    """The distinction that matters: `unavailable` is not `disabled`."""
    monkeypatch.setenv("CONVERSATION_MEMORY_ENABLED", "true")
    get_settings.cache_clear()
    threads._last_error = "ImportError: No module named 'psycopg_pool'"

    status = threads.checkpointer_status()
    assert status["state"] == "unavailable"
    assert "psycopg_pool" in str(status["reason"])


def test_build_records_the_failure(monkeypatch):
    """A degraded build must leave evidence, not just a log line."""
    monkeypatch.setenv("CONVERSATION_MEMORY_ENABLED", "true")
    get_settings.cache_clear()

    def _boom():
        raise ImportError("No module named 'psycopg_pool'")

    monkeypatch.setattr(threads, "_pool", _boom)

    assert threads.build_checkpointer(setup=False) is None
    assert threads.checkpointer_status()["state"] == "unavailable"
