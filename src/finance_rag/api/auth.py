"""Bearer-token authentication and per-route authorisation.

``/v1/ask`` spends model budget on every call and ``/v1/index`` writes to the
corpus, yet both were reachable by anyone who could resolve the load balancer.
This module is the gate in front of them.

Credentials are static API keys held in Parameter Store rather than tokens from
an identity provider, because there is no identity provider in front of this
service and every client is a machine: CI, the eval harness, and whatever proxy
fronts the UI. Each key carries the two things a request needs -- the tenant it
acts for and what it is allowed to do -- which is why :func:`tenant` can stay
the single seam that resolves the org. It now reads a verified credential
instead of a bare header.

Swapping in an IdP later means replacing :func:`authenticate` with one that
validates that provider's token and reads the same two facts out of its claims.
Nothing else in the application changes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Final

from fastapi import Depends, Header, HTTPException

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


class Scope:
    """What a credential is allowed to do.

    Split by consequence rather than by endpoint: ``ask`` spends money, ``index``
    mutates the corpus, ``read`` exposes other people's questions. A key issued
    to the eval harness needs the first and not the second.
    """

    ASK: Final = "ask"
    INDEX: Final = "index"
    READ: Final = "read"

    ALL: Final = frozenset({ASK, INDEX, READ})
    WILDCARD: Final = "*"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind one request."""

    org_id: str
    key_id: str
    scopes: frozenset[str]


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _parse_scopes(field: str, key_id: str) -> frozenset[str]:
    names = {s.strip().lower() for s in field.split("|") if s.strip()}
    if not names:
        raise ValueError(f"AUTH_API_KEYS key {key_id!r} grants no scopes")
    if Scope.WILDCARD in names:
        return frozenset(Scope.ALL)
    unknown = names - Scope.ALL
    if unknown:
        raise ValueError(
            f"AUTH_API_KEYS key {key_id!r} names unknown scopes {sorted(unknown)}; "
            f"valid scopes are {sorted(Scope.ALL)} or '*'"
        )
    return frozenset(names)


def parse_api_keys(raw: str) -> dict[str, Principal]:
    """Parse ``AUTH_API_KEYS`` into a digest -> principal table.

    Format is comma-separated ``key_id:org_id:scopes:secret``, where scopes are
    ``|``-separated. The secret is the last field and is split off with a bounded
    split, so a secret containing ``:`` survives intact.

    The table is keyed by SHA-256 of the secret so verification is a dictionary
    lookup on a fixed-width digest, rather than a comparison loop whose duration
    grows with the number of configured keys.

    Raises rather than skipping a bad entry: a key silently dropped at parse time
    is an outage that looks like a client bug.
    """
    table: dict[str, Principal] = {}
    for position, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 3)
        if len(parts) != 4:
            # Deliberately does not echo the entry: its fourth field is a secret.
            raise ValueError(
                f"AUTH_API_KEYS entry {position} is malformed; "
                "expected key_id:org_id:scopes:secret"
            )
        key_id, org_id, scope_field, secret = (p.strip() for p in parts)
        if not key_id or not org_id or not secret:
            raise ValueError(
                f"AUTH_API_KEYS entry {position} has an empty key_id, org_id or secret"
            )
        digest = _digest(secret)
        if digest in table:
            raise ValueError(
                f"AUTH_API_KEYS gives the same secret to {table[digest].key_id!r} "
                f"and {key_id!r}; revoking one would silently revoke both"
            )
        table[digest] = Principal(
            org_id=org_id,
            key_id=key_id,
            scopes=_parse_scopes(scope_field, key_id),
        )
    return table


# Keyed on the raw setting rather than being a module global, so changing the
# configuration -- in a test, or between processes -- produces a new table
# instead of serving a stale one.
@lru_cache(maxsize=4)
def _key_table(raw: str) -> dict[str, Principal]:
    return parse_api_keys(raw)


def _credential(authorization: str | None) -> str | None:
    """Extract the bearer value, or None if this is not a bearer header."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    return value.strip() or None


def _unauthorized(detail: str) -> HTTPException:
    # WWW-Authenticate is what makes a 401 well-formed, and tells the client
    # which scheme to retry with instead of guessing.
    return HTTPException(
        status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


def tenant(x_org_id: str | None = Header(default=None)) -> str:
    """Resolve the calling tenant from the header.

    This is now the *unauthenticated* path only: it applies when auth is off,
    which outside development requires setting ``AUTH_ENABLED=false`` on purpose.
    With auth on, the org comes from the verified credential in
    :func:`authenticate` and this header is ignored -- otherwise any valid key
    could name another tenant and read its corpus.
    """
    settings_ = get_settings()
    if not settings_.enforce_tenancy:
        return settings_.default_org_id
    return (x_org_id or settings_.default_org_id).strip() or settings_.default_org_id


def authenticate(
    authorization: str | None = Header(default=None),
    x_org_id: str | None = Header(default=None),
) -> Principal:
    """Resolve the caller from the Authorization header.

    With auth disabled this returns an anonymous principal holding every scope,
    which is what keeps local development and the test suite frictionless.
    :func:`verify_auth_configuration` is what stops that state reaching a
    deployment by accident.
    """
    settings_ = get_settings()

    if not settings_.auth_required:
        return Principal(
            org_id=tenant(x_org_id), key_id="anonymous", scopes=frozenset(Scope.ALL)
        )

    presented = _credential(authorization)
    if presented is None:
        raise _unauthorized("missing bearer credential")

    principal = _key_table(settings_.auth_api_keys).get(_digest(presented))
    if principal is None:
        # No key id to log: there is no verified identity to attribute this to,
        # and logging any part of the presented secret would put it in CloudWatch.
        logger.warning("auth_rejected", reason="unknown_key")
        raise _unauthorized("invalid credential")

    if not settings_.enforce_tenancy:
        # Single-tenant deployment: the storage layer writes the default org_id
        # regardless, so a key naming another org would produce a principal that
        # disagrees with every row it touches.
        principal = replace(principal, org_id=settings_.default_org_id)

    return principal


def require(*scopes: str) -> Callable[..., Principal]:
    """Dependency factory: authenticate, then demand every named scope.

    403 rather than 401 on a scope failure: the credential is valid and retrying
    with different credentials of the same kind will not help.
    """
    needed = frozenset(scopes)

    def dependency(principal: Principal = Depends(authenticate)) -> Principal:
        missing = needed - principal.scopes
        if missing:
            logger.warning(
                "auth_forbidden",
                key_id=principal.key_id,
                org_id=principal.org_id,
                missing=sorted(missing),
            )
            raise HTTPException(
                status_code=403,
                detail=f"credential lacks scope: {', '.join(sorted(missing))}",
            )
        return principal

    return dependency


def verify_auth_configuration() -> None:
    """Fail closed on a deployment that cannot authenticate anybody.

    Called from the lifespan, so a misconfigured task never reaches a healthy
    target group. Crash-looping is the intended outcome rather than a harsh one:
    an API that bills per call must not serve traffic it cannot attribute, and a
    task that starts anyway is how the endpoint stayed open in the first place.
    """
    settings_ = get_settings()
    development = settings_.app_env.strip().lower() == "development"

    if not settings_.auth_required:
        if development:
            logger.info("auth_disabled", reason="APP_ENV=development")
            return
        # Reaching here needs an explicit AUTH_ENABLED=false, which is a
        # deliberate act -- a deployment behind its own gateway is a real case.
        # Loud, because it is indistinguishable from the old behaviour otherwise.
        logger.warning(
            "auth_disabled_in_deployment",
            reason="AUTH_ENABLED=false outside development; /v1 is open to anyone who can reach it",
        )
        return

    # Parses eagerly: a malformed value must fail at startup, not on the first
    # request that happens to need a key.
    table = _key_table(settings_.auth_api_keys)
    if table:
        logger.info(
            "auth_enabled",
            keys=len(table),
            orgs=len({p.org_id for p in table.values()}),
        )
        return

    if development:
        logger.warning(
            "auth_enabled_without_keys",
            reason="AUTH_API_KEYS is empty; every /v1 request will be rejected",
        )
        return

    raise RuntimeError(
        "AUTH_ENABLED is on but AUTH_API_KEYS is empty: this task can authenticate "
        "nobody and every /v1 request would 401. Supply AUTH_API_KEYS, or set "
        "AUTH_ENABLED=false to serve without authentication on purpose."
    )
