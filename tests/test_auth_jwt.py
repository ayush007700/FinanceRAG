"""OIDC bearer tokens alongside API keys.

Tokens are signed with a real RSA key generated per test session, so the
signature path is exercised rather than mocked. Only the JWKS fetch is
replaced: the point of these tests is what we do with a key, not how PyJWT
fetches one.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from finance_rag.api import auth
from finance_rag.config import get_settings

ISSUER = "https://idp.example.test/"
AUDIENCE = "finance-rag"


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture(autouse=True)
def _jwt_configured(monkeypatch, keypair):
    """JWT on, API keys on, JWKS lookup answered by the test keypair."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_API_KEYS", "ci:default:ask|read:machine-secret-value-1234")
    monkeypatch.setenv("AUTH_JWT_JWKS_URL", "https://idp.example.test/.well-known/jwks.json")
    monkeypatch.setenv("AUTH_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("AUTH_JWT_ORG_CLAIM", "org_id")
    monkeypatch.setenv("AUTH_JWT_SCOPES_CLAIM", "scope")
    monkeypatch.setenv("ENFORCE_TENANCY", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    auth._key_table.cache_clear()

    _, public = keypair
    monkeypatch.setattr(auth, "_signing_key", lambda token: public)
    yield
    get_settings.cache_clear()
    auth._key_table.cache_clear()


def _token(private, **overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-42",
        "org_id": "acme",
        "scope": "ask read",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "test"})


def _auth(bearer: str) -> auth.Principal:
    return auth.authenticate(authorization=f"Bearer {bearer}", x_org_id=None)


# --- the happy path ---------------------------------------------------------


def test_valid_token_yields_a_person(keypair):
    private, _ = keypair
    p = _auth(_token(private))

    assert p.kind == "jwt"
    assert p.subject == "user-42"
    assert p.org_id == "acme"
    assert p.scopes == frozenset({"ask", "read"})


def test_api_keys_still_work_and_subject_is_the_key_id():
    p = _auth("machine-secret-value-1234")

    assert p.kind == "api_key"
    assert p.key_id == "ci"
    assert p.subject == "ci"
    assert p.org_id == "default"


def test_scopes_accept_a_list_claim(keypair):
    """Most providers emit roles/groups as a JSON array, not a space-joined string."""
    private, _ = keypair
    p = _auth(_token(private, scope=["index", "read"]))
    assert p.scopes == frozenset({"index", "read"})


def test_wildcard_scope_grants_everything(keypair):
    private, _ = keypair
    p = _auth(_token(private, scope="*"))
    assert p.scopes == auth.Scope.ALL


# --- what must be rejected ----------------------------------------------------


def test_expired_token_is_401(keypair):
    private, _ = keypair
    with pytest.raises(HTTPException) as exc:
        _auth(_token(private, exp=int(time.time()) - 10))
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail


def test_wrong_audience_is_401(keypair):
    """A token minted for another application must not authenticate here."""
    private, _ = keypair
    with pytest.raises(HTTPException) as exc:
        _auth(_token(private, aud="some-other-app"))
    assert exc.value.status_code == 401


def test_wrong_issuer_is_401(keypair):
    private, _ = keypair
    with pytest.raises(HTTPException) as exc:
        _auth(_token(private, iss="https://evil.example/"))
    assert exc.value.status_code == 401


def test_token_signed_by_another_key_is_401(keypair):
    """The signature check is real: a different private key must fail."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException) as exc:
        _auth(_token(other))
    assert exc.value.status_code == 401


def test_missing_tenant_claim_is_rejected_not_defaulted(keypair):
    """An unattributable token must not silently land in the default org."""
    private, _ = keypair
    with pytest.raises(HTTPException) as exc:
        _auth(_token(private, org_id=None))
    assert exc.value.status_code == 401
    assert "org_id" in exc.value.detail


def test_token_with_no_scopes_is_rejected_at_the_gate(keypair):
    private, _ = keypair
    with pytest.raises(HTTPException) as exc:
        _auth(_token(private, scope=None))
    assert exc.value.status_code == 401
    assert "scope" in exc.value.detail


def test_unreachable_jwks_is_503_not_401(monkeypatch, keypair):
    """Our fault, not the caller's: the status code must say so."""
    private, _ = keypair

    def _down(token):
        raise jwt.exceptions.PyJWKClientConnectionError("connection refused")

    monkeypatch.setattr(auth, "_signing_key", _down)
    with pytest.raises(HTTPException) as exc:
        _auth(_token(private))
    assert exc.value.status_code == 503


# --- tenant-aware authorization -----------------------------------------------


def test_tenant_comes_from_the_token_not_the_header(keypair):
    """X-Org-Id must be ignored: a valid token cannot name another tenant."""
    private, _ = keypair
    p = auth.authenticate(
        authorization=f"Bearer {_token(private, org_id='acme')}", x_org_id="victim-org"
    )
    assert p.org_id == "acme"


def test_single_tenant_deployment_overrides_the_token_org(monkeypatch, keypair):
    monkeypatch.setenv("ENFORCE_TENANCY", "false")
    monkeypatch.setenv("DEFAULT_ORG_ID", "default")
    get_settings.cache_clear()
    private, _ = keypair
    p = _auth(_token(private, org_id="acme"))
    assert p.org_id == "default"


def test_require_enforces_token_scopes(keypair):
    private, _ = keypair
    token = _token(private, scope="read")
    dep = auth.require(auth.Scope.INDEX)
    principal = _auth(token)
    with pytest.raises(HTTPException) as exc:
        dep(principal)
    assert exc.value.status_code == 403
    assert "index" in exc.value.detail


# --- startup configuration ----------------------------------------------------


def test_jwt_alone_is_a_valid_configuration(monkeypatch):
    monkeypatch.setenv("AUTH_API_KEYS", "")
    get_settings.cache_clear()
    auth._key_table.cache_clear()
    auth.verify_auth_configuration()


def test_jwks_without_issuer_and_audience_refuses_to_start(monkeypatch):
    """Signature-only validation accepts every token the provider ever signed."""
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc:
        auth.verify_auth_configuration()
    assert "AUDIENCE" in str(exc.value)


def test_neither_credential_kind_refuses_to_start(monkeypatch):
    monkeypatch.setenv("AUTH_API_KEYS", "")
    monkeypatch.setenv("AUTH_JWT_JWKS_URL", "")
    get_settings.cache_clear()
    auth._key_table.cache_clear()
    with pytest.raises(RuntimeError):
        auth.verify_auth_configuration()
