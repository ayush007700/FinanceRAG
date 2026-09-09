"""Per-credential rate limiting for the ``/v1`` surface.

:mod:`finance_rag.api.auth` exists because ``/v1/ask`` spends model budget on
every call. Authentication answers *who is spending*; it does not bound *how
much*. A valid key could, until this module, run the monthly budget out in a
loop -- and because the org is a property of the key, that spend is attributed
correctly to a tenant while still being unbounded.

Limits are per credential rather than per IP. An IP is not the unit that costs
money here: one key behind a NAT is one payer, and ten browsers sharing a key
are still one payer. Per-IP limiting would also be trivially defeated by the
machine clients this API is mostly for.

Windows are fixed rather than sliding. A sliding log is more accurate at the
boundary, but it stores one entry per request per key; a fixed window is two
integers and one round trip. At limits measured in tens per minute, the
boundary burst a fixed window permits -- up to 2x the limit across an instant
spanning two windows -- is not worth a per-request sorted set.

State lives in Redis when it is configured, because the service autoscales:
in-process counters would give each task its own limit, so scaling out would
raise the effective limit rather than hold it. Without Redis the limiter falls
back to per-process counters, which is correct only at one task -- a fallback
that is loudly logged rather than silently wrong.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Final

from fastapi import HTTPException

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)

# Scope -> (requests, window seconds). Split by what the scope costs, mirroring
# the reasoning in auth.Scope: `ask` spends model budget on every call, `index`
# launches a 2 vCPU task and rewrites the corpus, `read` touches only rows we
# already have.
DEFAULT_LIMITS: Final[dict[str, tuple[int, int]]] = {
    "ask": (30, 60),
    "index": (5, 3600),
    "read": (120, 60),
}


@dataclass(frozen=True)
class Decision:
    """The outcome of one limiter check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class _LocalWindows:
    """Per-process fallback counters.

    Correct only while the service runs a single task. Kept deliberately simple:
    this is the degraded path, and a sophisticated in-process limiter would
    invite someone to rely on it.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def incr(self, key: str, window: int, now: float) -> tuple[int, float]:
        bucket_start = now - (now % window)
        with self._lock:
            count, start = self._counts.get(key, (0, bucket_start))
            if start < bucket_start:
                count, start = 0, bucket_start
            count += 1
            self._counts[key] = (count, start)
            # Bounded by the number of live credentials, but a long-running
            # process with rotating keys would otherwise grow this forever.
            if len(self._counts) > 10_000:
                cutoff = now - window
                self._counts = {
                    k: v for k, v in self._counts.items() if v[1] >= cutoff
                }
            return count, start + window


_local = _LocalWindows()
_redis_client = None
_redis_failed = False


def _redis():
    """Return a Redis client, or None if unavailable.

    A connection failure disables Redis for the process rather than retrying on
    every request: the limiter sits in the request path, and a limiter that adds
    a connection timeout to each call is worse than the burst it prevents.
    """
    global _redis_client, _redis_failed
    if _redis_failed:
        return None
    if _redis_client is not None:
        return _redis_client
    settings = get_settings()
    if not settings.rate_limit_use_redis:
        _redis_failed = True
        return None
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url, socket_timeout=0.25, socket_connect_timeout=0.25
        )
        client.ping()
        _redis_client = client
        logger.info("ratelimit_backend", backend="redis")
        return client
    except Exception as exc:  # noqa: BLE001 - any failure means "no Redis"
        _redis_failed = True
        logger.warning(
            "ratelimit_redis_unavailable",
            error=str(exc),
            reason="falling back to per-process counters; limits are per task, "
            "so scaling out raises the effective limit",
        )
        return None


def _limits_for(scopes: frozenset[str]) -> tuple[int, int]:
    """The tightest limit among the scopes a route demands.

    A route requiring `index` gets the index limit even though the key may also
    hold `ask`: the limit belongs to the action being taken, not to everything
    the credential could do.
    """
    settings = get_settings()
    overrides = {
        "ask": (settings.rate_limit_ask, 60),
        "index": (settings.rate_limit_index, 3600),
        "read": (settings.rate_limit_read, 60),
    }
    candidates = [overrides[s] for s in scopes if s in overrides] or [
        overrides["read"]
    ]
    # Tightest = fewest requests per second of window.
    return min(candidates, key=lambda lw: lw[0] / lw[1])


def check(key_id: str, scopes: frozenset[str]) -> Decision:
    """Count one request against ``key_id`` and decide whether to allow it."""
    limit, window = _limits_for(scopes)
    now = time.time()
    # Annotated because the two backends disagree on the type: Redis computes an
    # integral bucket edge, the local one carries a float clock forward.
    reset_at: float
    bucket = f"rl:{key_id}:{'-'.join(sorted(scopes))}:{int(now // window)}"

    client = _redis()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.incr(bucket)
            pipe.expire(bucket, window)
            count = int(pipe.execute()[0])
            reset_at = (int(now // window) + 1) * window
        except Exception as exc:  # noqa: BLE001
            # Fail open on a limiter fault. Refusing traffic because the
            # limiter broke turns a cost control into an outage, and the
            # failure is logged where an alarm can see it.
            logger.warning("ratelimit_backend_error", error=str(exc))
            return Decision(True, limit, limit, 0)
    else:
        count, reset_at = _local.incr(bucket, window, now)

    remaining = max(0, limit - count)
    return Decision(
        allowed=count <= limit,
        limit=limit,
        remaining=remaining,
        retry_after=max(1, int(reset_at - now)),
    )


def enforce(key_id: str, org_id: str, scopes: frozenset[str]) -> Decision:
    """Raise 429 when ``key_id`` is over its limit, otherwise return the decision."""
    if not get_settings().rate_limit_enabled:
        return Decision(True, 0, 0, 0)

    decision = check(key_id, scopes)
    if decision.allowed:
        return decision

    logger.warning(
        "ratelimit_exceeded",
        key_id=key_id,
        org_id=org_id,
        scopes=sorted(scopes),
        limit=decision.limit,
    )
    raise HTTPException(
        status_code=429,
        detail=f"rate limit exceeded: {decision.limit} requests per "
        f"{'hour' if decision.retry_after > 60 else 'minute'}",
        # Retry-After is what makes a 429 actionable: without it a client can
        # only guess, and guessing clients retry immediately.
        headers={
            "Retry-After": str(decision.retry_after),
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": "0",
        },
    )
