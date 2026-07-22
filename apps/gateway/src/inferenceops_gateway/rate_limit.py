"""Per-key token-bucket rate limiting for the gateway (todo 11).

``TokenBucketRateLimiter`` is the pure core: one continuously-refilling token
bucket per identity key. It is safe under both threads and asyncio — a
``threading.Lock`` guards a small await-free critical section, which is correct
on an event loop AND across TestClient's worker threads. The clock is
injectable, so tests control time instead of sleeping. ``RateLimitMiddleware``
is the ASGI edge: it keys the bucket by the authenticated principal published
by ``auth.py`` (falling back to the client IP) and rejects over-limit requests
with 429 plus an RFC 9110 ``Retry-After`` (whole seconds until a token
refills, at least 1). ``/healthz`` is exempt so probes are never throttled.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from inferenceops_gateway.auth import HEALTHZ_PATH, Principal

RETRY_AFTER_HEADER: Final = "Retry-After"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The outcome of one rate-limit check."""

    allowed: bool
    retry_after_s: float  # 0.0 when allowed; seconds until a token refills otherwise
    remaining: float  # tokens left after the decision (>= 0)


@dataclass(slots=True)
class _Bucket:
    """Mutable bucket state for one identity key."""

    tokens: float
    refilled_at: float


class TokenBucketRateLimiter:
    """Continuously-refilling token bucket keyed per identity (API key or IP)."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """A bucket refills at ``rate`` tokens/second up to ``capacity``.

        ``capacity`` defaults to one second of burst (``rate``), but never below
        1.0 — a sub-1 RPS limit must still admit a single request, then make the
        caller wait ``1 / rate`` seconds for the next.
        """
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self._rate = rate
        self._capacity = capacity if capacity is not None else max(rate, 1.0)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def consume(self, key: str, tokens: float = 1.0) -> RateLimitDecision:
        """Consume ``tokens`` for ``key``, or report how long until one refills."""
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, refilled_at=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.refilled_at)
                bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._rate)
                bucket.refilled_at = now
            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return RateLimitDecision(allowed=True, retry_after_s=0.0, remaining=bucket.tokens)
            deficit = tokens - bucket.tokens
            return RateLimitDecision(
                allowed=False, retry_after_s=deficit / self._rate, remaining=bucket.tokens
            )


def limit_key(scope: Scope) -> str:
    """The bucket key: the authenticated principal's key id, else the client IP."""
    existing: dict[str, object] | None = scope.get("state")
    if isinstance(existing, dict):
        principal = existing.get("principal")
        if isinstance(principal, Principal):
            return f"key:{principal.api_key_id}"
    client = Request(scope).client
    return f"ip:{client.host if client is not None else 'unknown'}"


class RateLimitMiddleware:
    """Pure-ASGI middleware: 429 + Retry-After when the caller's bucket is empty."""

    def __init__(self, app: ASGIApp, limiter: TokenBucketRateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pass lifespan/websocket through untouched
            await self.app(scope, receive, send)
            return
        if Request(scope).url.path == HEALTHZ_PATH:
            await self.app(scope, receive, send)
            return
        decision = self.limiter.consume(limit_key(scope))
        if not decision.allowed:
            retry_after = str(max(1, math.ceil(decision.retry_after_s)))
            response = JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={RETRY_AFTER_HEADER: retry_after},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
