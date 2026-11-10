"""Prometheus metrics for the gateway.

One counter records requests by route and status; one histogram records the
wall time of each request by route. Both live on a private `CollectorRegistry`
built inside `create_app`, so tests that build several applications do not
collide on the process-wide default registry.

The route label is bounded. Proxied `/v1` paths keep their own path so
`/v1/models` and `/v1/chat/completions` stay distinguishable; every other
known route uses the matched path; anything unmatched collapses to one
placeholder so a scanner cannot mint new series. The label carries the path
only, never the query string or an API key.
"""

from __future__ import annotations

import logging
import time
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGGER = logging.getLogger(__name__)
EXPOSITION_CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
PROXIED_PREFIX: Final = "/v1/"
STATIC_ROUTES: Final[frozenset[str]] = frozenset({"/healthz", "/readyz", "/metrics"})
UNMATCHED_ROUTE: Final = "<unmatched>"


def route_label(scope: Scope) -> str:
    """Bound the route label for one request.

    A matched route contributes its path template. Proxied `/v1` traffic has no
    per-endpoint template, so the raw path stands in; the set of llama.cpp
    endpoints is small and the query string is not part of the path.
    """
    path = scope.get("path", "")
    if not isinstance(path, str):
        return UNMATCHED_ROUTE
    if path.startswith(PROXIED_PREFIX):
        return path

    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template

    if path in STATIC_ROUTES:
        return path
    return UNMATCHED_ROUTE


class GatewayMetrics:
    """The gateway's counter, histogram, and the registry that holds them."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "gateway_requests_total",
            "HTTP requests handled by the gateway.",
            ["route", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "gateway_request_duration_seconds",
            "Wall time spent handling a gateway request.",
            ["route"],
            registry=self.registry,
        )

    def record(self, route: str, status: int, duration_s: float) -> None:
        """Count one request and observe its wall time."""
        self.requests.labels(route=route, status=str(status)).inc()
        self.duration.labels(route=route).observe(duration_s)

    def render(self) -> bytes:
        """The registry in Prometheus text exposition format."""
        return generate_latest(self.registry)


class MetricsMiddleware:
    """Time each request, count it by route and status, and log one line.

    The middleware sits outside auth and the rate limiter, so a 401 and a 429
    are counted too. The status is read from the response start message, which
    every downstream layer emits, including the short-circuit responses.
    """

    def __init__(self, app: ASGIApp, metrics: GatewayMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                raw_status = message.get("status")
                if isinstance(raw_status, int):
                    status = raw_status
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            # A route that raises has no response start message; record the 500
            # before Starlette's error middleware turns the exception into one.
            duration_s = time.perf_counter() - started
            route = route_label(scope)
            self.metrics.record(route, status, duration_s)
            LOGGER.info(
                "%s %s %d %.1fms",
                scope.get("method", "?"),
                route,
                status,
                duration_s * 1000.0,
            )
