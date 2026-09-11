"""/metrics is no longer the one route without a credential check."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finance_rag.api import auth
from finance_rag.config import get_settings


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ENABLE_PROMETHEUS", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv(
        "AUTH_API_KEYS",
        "prom:default:metrics:scrape-secret-0000,"
        "reader:default:read:read-secret-0000,"
        "admin:default:*:admin-secret-0000",
    )
    get_settings.cache_clear()
    auth._key_table.cache_clear()
    from finance_rag.api.app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    get_settings.cache_clear()
    auth._key_table.cache_clear()


def test_metrics_without_credential_is_401(client):
    assert client.get("/metrics").status_code == 401


def test_metrics_with_the_metrics_scope_is_200(client):
    r = client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-0000"})
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_read_scope_is_not_enough(client):
    """A key that can read audits must not thereby read metrics, nor vice versa."""
    r = client.get("/metrics", headers={"Authorization": "Bearer read-secret-0000"})
    assert r.status_code == 403
    assert "metrics" in r.json()["detail"]


def test_metrics_scope_cannot_read_audits(client):
    r = client.get("/v1/audit", headers={"Authorization": "Bearer scrape-secret-0000"})
    assert r.status_code == 403


def test_wildcard_covers_metrics(client):
    r = client.get("/metrics", headers={"Authorization": "Bearer admin-secret-0000"})
    assert r.status_code == 200


def test_health_stays_open(client):
    """The ALB health check has no credential; it must keep working."""
    assert client.get("/health").status_code == 200
