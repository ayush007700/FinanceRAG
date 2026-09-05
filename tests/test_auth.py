"""Authentication and per-scope authorisation on the /v1 surface.

The property under test is not "a header is parsed" but "an unauthenticated
caller cannot spend model budget, write to the corpus, or read another tenant's
questions". Every test here is written against the HTTP surface for that reason,
except the parser tests, where the failure mode is a misconfigured deployment
rather than a rejected request.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finance_rag.api.auth import (
    Principal,
    Scope,
    _key_table,
    authenticate,
    parse_api_keys,
    verify_auth_configuration,
)
from finance_rag.config import get_settings

# key_id:org_id:scopes:secret
ACME_ALL = "acme-ci:acme:*:secret-acme"
ACME_ASK = "acme-ui:acme:ask:secret-ask-only"
GLOBEX_ALL = "globex-ci:globex:*:secret-globex"
KEYS = f"{ACME_ALL},{ACME_ASK},{GLOBEX_ALL}"


@pytest.fixture
def auth_on(monkeypatch):
    """Turn auth on with a known key table, as a deployment would have it."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEYS", KEYS)
    monkeypatch.setenv("ENFORCE_TENANCY", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(auth_on):
    from finance_rag.api.app import app

    # The lifespan is deliberately not entered: it reaps jobs against a live
    # database, and no test here reaches a route that needs one. Server
    # exceptions are returned rather than raised so that a handler failing for
    # want of a database cannot be mistaken for an auth result.
    return TestClient(app, raise_server_exceptions=False)


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# --- parsing ---------------------------------------------------------------


def test_key_binds_an_org_and_a_scope_set():
    table = parse_api_keys(ACME_ASK)
    principal = next(iter(table.values()))
    assert principal.org_id == "acme"
    assert principal.key_id == "acme-ui"
    assert principal.scopes == frozenset({Scope.ASK})


def test_wildcard_expands_to_every_scope():
    principal = next(iter(parse_api_keys(ACME_ALL).values()))
    assert principal.scopes == Scope.ALL


def test_secret_may_contain_colons():
    table = parse_api_keys("k:org:ask:aa:bb:cc")
    assert len(table) == 1
    assert table == parse_api_keys("k:org:ask:aa:bb:cc")


@pytest.mark.parametrize(
    "raw",
    [
        "keyonly",
        "k:org:ask",  # no secret
        "k::ask:secret",  # no org
        ":org:ask:secret",  # no key id
        "k:org::secret",  # no scopes
        "k:org:admin:secret",  # scope that does not exist
    ],
)
def test_malformed_configuration_is_rejected_not_skipped(raw):
    """A key silently dropped at parse time is an outage that looks like a
    client bug, so parsing raises instead of ignoring the entry."""
    with pytest.raises(ValueError):
        parse_api_keys(raw)


def test_a_shared_secret_is_rejected():
    """Two ids on one secret means revoking either revokes both."""
    with pytest.raises(ValueError):
        parse_api_keys("a:org1:ask:same,b:org2:ask:same")


def test_parse_errors_never_echo_the_secret():
    """Configuration errors land in CloudWatch; the secret must not go with them."""
    with pytest.raises(ValueError) as excinfo:
        parse_api_keys("a:org1:ask:topsecret,b:org2:ask:topsecret")
    assert "topsecret" not in str(excinfo.value)


# --- rejection -------------------------------------------------------------


def test_ask_without_a_credential_is_401(client):
    res = client.post("/v1/ask", json={"query": "what is the R&D credit rate?"})
    assert res.status_code == 401
    # Without this header the 401 is not a well-formed challenge.
    assert res.headers["www-authenticate"] == "Bearer"


def test_index_without_a_credential_is_401(client):
    res = client.post("/v1/index", json={"paths": ["data/corpus"]})
    assert res.status_code == 401


def test_unknown_key_is_401(client):
    res = client.post("/v1/ask", json={"query": "anything at all"}, headers=bearer("nope"))
    assert res.status_code == 401


def test_non_bearer_scheme_is_401(client):
    res = client.post(
        "/v1/ask",
        json={"query": "anything at all"},
        headers={"Authorization": "Basic secret-acme"},
    )
    assert res.status_code == 401


def test_the_raw_secret_is_not_a_credential_without_the_scheme(client):
    res = client.post(
        "/v1/ask",
        json={"query": "anything at all"},
        headers={"Authorization": "secret-acme"},
    )
    assert res.status_code == 401


def test_audit_and_eval_history_are_no_longer_public(client):
    """Both endpoints previously had no auth and no tenancy check at all."""
    assert client.get("/v1/audit").status_code == 401
    assert client.get("/v1/eval/runs").status_code == 401


def test_health_stays_open(client):
    """The ALB health check presents no credential, so gating it would fail
    every deployment. Whether the dependencies are reachable is another test."""
    assert client.get("/health").status_code != 401


# --- scopes ----------------------------------------------------------------


def test_ask_only_key_cannot_index(client):
    res = client.post(
        "/v1/index", json={"paths": ["data/corpus"]}, headers=bearer("secret-ask-only")
    )
    # 403, not 401: the credential is valid and retrying with it will not help.
    assert res.status_code == 403
    assert "index" in res.json()["detail"]


def test_ask_only_key_cannot_upload(client):
    res = client.post(
        "/v1/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers=bearer("secret-ask-only"),
    )
    assert res.status_code == 403


def test_ask_only_key_cannot_read_the_audit_trail(client):
    assert client.get("/v1/audit", headers=bearer("secret-ask-only")).status_code == 403


def test_a_scoped_key_passes_the_gate(client, monkeypatch):
    """Past the gate the request reaches the handler, which is all this asserts:
    the agent itself is exercised elsewhere and would cost a model call here."""
    seen: dict[str, str] = {}

    def fake_dispatch(paths, org_id):
        seen["org_id"] = org_id

        class D:
            job_id, runner, task_arn, error = 1, "inline", None, None

        return D()

    monkeypatch.setattr(
        "finance_rag.pipeline.launcher.dispatch_index_job", fake_dispatch
    )
    res = client.post(
        "/v1/index", json={"paths": ["data/corpus"]}, headers=bearer("secret-acme")
    )
    assert res.status_code == 202
    assert seen["org_id"] == "acme"


# --- tenancy ---------------------------------------------------------------


def test_org_comes_from_the_key_not_the_header(auth_on):
    """The whole point of the change. A valid key must not be able to name
    another tenant and read its corpus."""
    principal = authenticate(
        authorization="Bearer secret-acme", x_org_id="globex"
    )
    assert principal.org_id == "acme"


def test_each_key_carries_its_own_org(auth_on):
    assert authenticate(authorization="Bearer secret-globex", x_org_id=None).org_id == "globex"


def test_single_tenant_deployment_collapses_every_key_to_the_default(monkeypatch):
    """With tenancy off the storage layer writes default_org_id regardless, so a
    principal naming another org would disagree with every row it touches."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEYS", KEYS)
    monkeypatch.setenv("ENFORCE_TENANCY", "false")
    get_settings.cache_clear()
    try:
        principal = authenticate(authorization="Bearer secret-acme", x_org_id=None)
        assert principal.org_id == get_settings().default_org_id
    finally:
        get_settings.cache_clear()


# --- configuration ---------------------------------------------------------


def test_auth_is_on_by_default_outside_development(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        assert get_settings().auth_required is True
    finally:
        get_settings.cache_clear()


def test_auth_is_off_by_default_in_development(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    try:
        assert get_settings().auth_required is False
    finally:
        get_settings.cache_clear()


def test_deployment_without_keys_refuses_to_start(monkeypatch):
    """Fail closed: a task that can authenticate nobody must not reach a healthy
    target group, because that is how the endpoint stayed open in the first
    place."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEYS", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="AUTH_API_KEYS"):
            verify_auth_configuration()
    finally:
        get_settings.cache_clear()


def test_deployment_with_a_malformed_key_refuses_to_start(monkeypatch):
    """Surfaces at startup rather than on the first request needing that key."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEYS", "garbage")
    get_settings.cache_clear()
    _key_table.cache_clear()
    try:
        with pytest.raises(ValueError):
            verify_auth_configuration()
    finally:
        get_settings.cache_clear()
        _key_table.cache_clear()


def test_explicitly_disabling_auth_is_allowed_but_not_silent(monkeypatch):
    """A deployment behind its own gateway is a real case, so this warns rather
    than raising -- but it must not be indistinguishable from the old default."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        verify_auth_configuration()  # does not raise
        assert get_settings().auth_required is False
    finally:
        get_settings.cache_clear()


def test_disabled_auth_still_yields_a_usable_principal(monkeypatch):
    """Local development keeps working exactly as it did, header and all."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.setenv("ENFORCE_TENANCY", "true")
    get_settings.cache_clear()
    try:
        principal = authenticate(authorization=None, x_org_id="acme")
        assert isinstance(principal, Principal)
        assert principal.org_id == "acme"
        assert principal.scopes == Scope.ALL
    finally:
        get_settings.cache_clear()
