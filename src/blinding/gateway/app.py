"""Build the gateway application around one llama.cpp upstream.

`create_app` wires the proxy, the health and readiness routes, the Prometheus
endpoint, and the middleware. Middleware is registered innermost first because
Starlette wraps the user stack in reverse registration order: the rate limiter
is added first, then auth, then metrics, so the request flow is metrics and
logging, auth, rate limit, route. A rejected request is therefore counted and
logged, and a 401 never consumes a rate-limit token.

The upstream `httpx.AsyncClient` is created here but does no I/O until the
first request. `transport` is the test seam: production leaves the argument
unset and uses the network, tests pass an `httpx.MockTransport`. The lifespan
closes the client on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final

import httpx
from fastapi import FastAPI
from starlette.responses import JSONResponse, Response
from starlette.status import (
    HTTP_200_OK,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from blinding import __version__
from blinding.gateway.auth import AuthMiddleware
from blinding.gateway.metrics import EXPOSITION_CONTENT_TYPE, GatewayMetrics, MetricsMiddleware
from blinding.gateway.proxy import PROXY_MOUNT_PATH, UPSTREAM_TIMEOUT_S, LLMProxy
from blinding.gateway.rate_limit import RateLimitMiddleware, TokenBucketRateLimiter
from blinding.gateway.settings import GatewaySettings, settings_from_env

MODELS_PATH: Final = "/v1/models"
NO_KEYS_MESSAGE: Final = (
    "refusing to start: BLINDING_API_KEYS must name at least one API key, "
    "because a gateway with no keys admits every caller"
)


def _upstream_base(llm_url: str) -> str:
    """Add the trailing slash httpx needs to merge relative request paths."""
    return llm_url.rstrip("/") + "/"


def create_app(
    settings: GatewaySettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the gateway application.

    `settings` defaults to reading the `BLINDING_*` environment. An empty API
    key set raises `ValueError` here rather than yielding an open gateway.
    """
    cfg = settings if settings is not None else settings_from_env()
    if not cfg.api_keys:
        raise ValueError(NO_KEYS_MESSAGE)

    client = httpx.AsyncClient(
        base_url=_upstream_base(cfg.llm_url),
        transport=transport,
        timeout=httpx.Timeout(UPSTREAM_TIMEOUT_S),
    )
    metrics = GatewayMetrics()
    limiter = TokenBucketRateLimiter(cfg.rate_limit_rps)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        await client.aclose()

    app = FastAPI(title="Blinding Gateway", version=__version__, lifespan=lifespan)

    # The proxy owns every method under /v1. The explicit health, readiness,
    # and metrics routes below sit on the root path.
    app.mount(PROXY_MOUNT_PATH, LLMProxy(client))

    async def healthz() -> dict[str, str]:
        """Liveness: the process answered, nothing upstream is needed."""
        return {"status": "ok"}

    async def readyz() -> Response:
        """Readiness: the upstream serves its model list."""
        try:
            upstream = await client.get(MODELS_PATH)
        except httpx.HTTPError:
            return JSONResponse({"status": "unavailable"}, status_code=HTTP_503_SERVICE_UNAVAILABLE)
        if upstream.status_code == HTTP_200_OK:
            return JSONResponse({"status": "ok"}, status_code=HTTP_200_OK)
        return JSONResponse({"status": "unavailable"}, status_code=HTTP_503_SERVICE_UNAVAILABLE)

    async def metrics_endpoint() -> Response:
        """The gateway's own counters, not the upstream's."""
        return Response(content=metrics.render(), media_type=EXPOSITION_CONTENT_TYPE)

    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route("/readyz", readyz, methods=["GET"])
    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"])

    # add_middleware wraps the application in reverse: first added is innermost.
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(AuthMiddleware, valid_keys=cfg.api_keys)
    app.add_middleware(MetricsMiddleware, metrics=metrics)

    return app
