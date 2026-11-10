"""API-key authentication for the gateway.

`AuthMiddleware` is the ASGI edge: a request without a known `X-API-Key` header
gets a 401 JSON body before any route runs or any rate-limit token is taken.
Health and metrics stay open so a liveness probe and a Prometheus scrape need
no credentials. Every other path, `/readyz` included, needs a key.
"""

from __future__ import annotations

from typing import Final

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY_HEADER: Final = "x-api-key"
OPEN_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/metrics"})


class AuthMiddleware:
    """Reject a request that carries no API key or an unknown one."""

    def __init__(self, app: ASGIApp, valid_keys: frozenset[str]) -> None:
        self.app = app
        self.valid_keys = valid_keys

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Websocket and lifespan scopes do not carry an X-API-Key header.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        api_key = Request(scope).headers.get(API_KEY_HEADER)
        if api_key is None:
            await _reject(scope, receive, send, "missing X-API-Key header")
            return
        if api_key not in self.valid_keys:
            await _reject(scope, receive, send, "invalid API key")
            return

        await self.app(scope, receive, send)


async def _reject(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
    """Write the 401 body, naming what the caller has to fix."""
    response = JSONResponse({"detail": detail}, status_code=HTTP_401_UNAUTHORIZED)
    await response(scope, receive, send)
