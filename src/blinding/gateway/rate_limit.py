"""In-memory token-bucket rate limiting for the gateway.

`TokenBucketRateLimiter` is the pure core: one bucket per API key, refilled at
`rate` tokens per second up to one second of burst. The bucket map and its lock
live in one event loop, so an `asyncio.Lock` is enough. `RateLimitMiddleware`
is the ASGI edge: an over-limit request gets a 429 with an integer `Retry-After`
before it reaches a route. Health and metrics stay outside the bucket so a
probe and a scrape cannot be throttled.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.status import HTTP_429_TOO_MANY_REQUESTS
from starlette.types import ASGIApp, Receive, Scope, Send

from blinding.gateway.auth import OPEN_PATHS

API_KEY_HEADER: Final = "x-api-key"
RETRY_AFTER_HEADER: Final = "Retry-After"
MIN_RETRY_AFTER_S: Final = 1
MIN_CAPACITY: Final = 1.0
UNKNOWN_CLIENT: Final = "unknown"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The outcome of one rate-limit check."""

    allowed: bool
    retry_after_s: float


@dataclass(slots=True)
class _Bucket:
    """Mutable token state for one API key."""

    tokens: float
    refilled_at: float


class TokenBucketRateLimiter:
    """A continuously refilling token bucket for each caller identity."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Refill at `rate` tokens per second, holding at most `capacity`.

        `capacity` defaults to one second of burst, never below one token: a
        limit under one request per second must still admit a single request,
        then make the caller wait `1 / rate` seconds for the next one.
        """
        if rate <= 0:
            raise ValueError("rate limit must be positive")
        self._rate = rate
        self._capacity = max(rate, MIN_CAPACITY) if capacity is None else capacity
        if self._capacity <= 0:
            raise ValueError("rate limit capacity must be positive")
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def consume(self, key: str, tokens: float = 1.0) -> RateLimitDecision:
        """Take `tokens` from the bucket for `key`, or report the wait."""
        async with self._lock:
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
                return RateLimitDecision(allowed=True, retry_after_s=0.0)

            deficit = tokens - bucket.tokens
            return RateLimitDecision(allowed=False, retry_after_s=deficit / self._rate)


def limit_key(scope: Scope) -> str:
    """Bucket key for a request: the API key, or the client address as fallback.

    Auth runs before the rate limiter, so ordinary traffic is bucketed per key.
    The client address only appears if the middleware is used on its own.
    """
    api_key = Request(scope).headers.get(API_KEY_HEADER)
    if api_key is not None:
        return f"key:{api_key}"

    client = Request(scope).client
    host = client.host if client is not None else UNKNOWN_CLIENT
    return f"ip:{host}"


class RateLimitMiddleware:
    """Reject an over-limit request with 429 and an integer Retry-After."""

    def __init__(self, app: ASGIApp, limiter: TokenBucketRateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        decision = await self.limiter.consume(limit_key(scope))
        if not decision.allowed:
            retry_after = max(MIN_RETRY_AFTER_S, math.ceil(decision.retry_after_s))
            response = JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                headers={RETRY_AFTER_HEADER: str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
