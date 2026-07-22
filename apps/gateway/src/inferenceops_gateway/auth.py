"""API-key authentication for the gateway (todo 11).

``authenticate`` is the pure core: it validates the presented key against the
configured set (``config.py``) and returns a ``Principal`` or raises
``InvalidApiKeyError`` — failing closed when the key set is empty.
``AuthMiddleware`` is the ASGI edge: it reads ``X-API-Key``, rejects a missing
or unknown key with 401, and publishes the authenticated principal into
``scope["state"]`` so the rate limiter can key its bucket per API key.
``/healthz`` is exempt so k8s/docker liveness probes need no credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY_HEADER: Final = "x-api-key"
HEALTHZ_PATH: Final = "/healthz"


class InvalidApiKeyError(Exception):
    """Raised when the presented API key is missing or not in the valid set."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller identity."""

    api_key_id: str
    tenant: str


def authenticate(api_key: str | None, valid_keys: frozenset[str]) -> Principal:
    """Validate ``api_key`` and return the authenticated principal.

    Raises ``InvalidApiKeyError`` when the key is absent or unknown. An empty
    ``valid_keys`` rejects every request (fail closed).
    """
    if api_key is None:
        raise InvalidApiKeyError("missing X-API-Key header")
    if api_key not in valid_keys:
        raise InvalidApiKeyError("invalid API key")
    return Principal(api_key_id=api_key, tenant="default")


class AuthMiddleware:
    """Pure-ASGI middleware enforcing API-key auth on every path except /healthz."""

    def __init__(self, app: ASGIApp, valid_keys: frozenset[str]) -> None:
        self.app = app
        self.valid_keys = valid_keys

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pass lifespan/websocket through untouched
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.url.path == HEALTHZ_PATH:
            await self.app(scope, receive, send)
            return
        try:
            principal = authenticate(request.headers.get(API_KEY_HEADER), self.valid_keys)
        except InvalidApiKeyError as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=401)
            await response(scope, receive, send)
            return
        existing: dict[str, object] | None = scope.get("state")
        state = dict(existing) if isinstance(existing, dict) else {}
        state["principal"] = principal
        scope["state"] = state
        await self.app(scope, receive, send)
