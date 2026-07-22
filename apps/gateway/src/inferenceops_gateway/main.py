"""Gateway entrypoint: wires middleware + routes into the FastAPI app (todo 11).

Request flow, outermost first: telemetry -> auth -> rate-limit -> route.
``add_middleware`` wraps in REVERSE registration order, so the rate limiter is
registered first (innermost) and telemetry last (outermost): an unauthenticated
request is spanned and rejected with 401 before it can consume a rate-limit
token, and an authenticated one is spanned, admitted, and bucketed before it
reaches a route.

The upstream httpx client is created at build time (no I/O until the first
request) and closed on lifespan shutdown. ``transport`` and ``telemetry`` are
injection seams: tests pass ``httpx.MockTransport`` and an in-memory-span
``Telemetry``; production leaves both None (real network, OTLP iff
``OTEL_ENDPOINT`` is configured).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final

import httpx
from fastapi import FastAPI

from inferenceops_gateway.auth import AuthMiddleware
from inferenceops_gateway.config import GatewaySettings, get_settings
from inferenceops_gateway.rate_limit import RateLimitMiddleware, TokenBucketRateLimiter
from inferenceops_gateway.routes import build_route_table
from inferenceops_gateway.routes.proxy import UPSTREAM_TIMEOUT_S
from inferenceops_gateway.telemetry import Telemetry, TelemetryMiddleware, init_telemetry

SERVICE_NAME: Final = "inferenceops-gateway"


def create_app(
    settings: GatewaySettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    telemetry: Telemetry | None = None,
) -> FastAPI:
    """Construct the gateway FastAPI application."""
    cfg = settings if settings is not None else get_settings()
    tel = telemetry if telemetry is not None else init_telemetry(SERVICE_NAME, cfg.otel_endpoint)
    client = httpx.AsyncClient(
        base_url=cfg.model_server_url,
        transport=transport,
        timeout=httpx.Timeout(UPSTREAM_TIMEOUT_S),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        await client.aclose()

    app = FastAPI(title="InferenceOps Gateway", lifespan=lifespan)

    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.include_router(build_route_table(client))

    limiter = TokenBucketRateLimiter(rate=cfg.rate_limit_rps)
    # Request flow: telemetry -> auth -> rate-limit -> route; add_middleware
    # wraps in reverse, so register the innermost middleware first.
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(AuthMiddleware, valid_keys=cfg.api_keys)
    app.add_middleware(TelemetryMiddleware, telemetry=tel)
    return app


app = create_app()
