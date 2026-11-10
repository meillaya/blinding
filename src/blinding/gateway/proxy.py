"""Reverse proxy for the OpenAI-compatible `/v1` surface.

`LLMProxy` is mounted at `/v1` and forwards every method, the query string, the
request body, and the upstream status and body to the configured llama.cpp
server. Gateway credentials and hop-by-hop headers are dropped in both
directions; httpx decodes a compressed upstream body, so the response is sent
without its original `Content-Encoding`.

An unreachable upstream becomes 502 and a slow one becomes 504. The proxy never
lets an upstream failure escape as an unhandled exception.
"""

from __future__ import annotations

from typing import Final

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_502_BAD_GATEWAY, HTTP_504_GATEWAY_TIMEOUT
from starlette.types import Receive, Scope, Send

PROXY_MOUNT_PATH: Final = "/v1"
UPSTREAM_TIMEOUT_S: Final = 120.0

# RFC 7230 hop-by-hop headers. A proxy must consume these and must not forward
# them; this list is also applied to the headers named by a Connection header.
HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Host and Content-Length belong to the connection to the upstream: httpx sets
# both for the forwarded request. X-API-Key is the gateway's own credential and
# must never reach the llama.cpp server.
REQUEST_HEADER_DROP: Final[frozenset[str]] = HOP_BY_HOP_HEADERS | {
    "host",
    "content-length",
    "x-api-key",
}

# httpx returns the body decoded, so the original framing headers are stale.
RESPONSE_HEADER_DROP: Final[frozenset[str]] = HOP_BY_HOP_HEADERS | {
    "content-length",
    "content-encoding",
}


def _connection_tokens(raw: str) -> frozenset[str]:
    """Header names listed by a Connection header, lowercased."""
    return frozenset(token.strip().lower() for token in raw.split(",") if token.strip())


def _forward_headers(request: Request) -> dict[str, str]:
    """Request headers that may reach the upstream."""
    connection_tokens = _connection_tokens(request.headers.get("connection", ""))
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in REQUEST_HEADER_DROP and name.lower() not in connection_tokens
    }


def _passthrough_headers(response: httpx.Response) -> dict[str, str]:
    """Upstream response headers that may reach the caller."""
    connection_tokens = _connection_tokens(response.headers.get("connection", ""))
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in RESPONSE_HEADER_DROP and name.lower() not in connection_tokens
    }


def request_path(scope: Scope) -> str:
    """The full request path, before the mount removed its prefix.

    `raw_path` is the request target the client sent, so the mount prefix is
    still on it. The `root_path` fallback covers a server that omits `raw_path`.
    """
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes) and raw_path:
        return raw_path.decode("latin-1")

    root_path = scope.get("root_path", "")
    path = scope.get("path", "")
    if isinstance(root_path, str) and isinstance(path, str):
        if root_path and not path.startswith(root_path):
            return root_path + path
        return path
    return PROXY_MOUNT_PATH


async def _send_error(
    scope: Scope, receive: Receive, send: Send, status_code: int, detail: str
) -> None:
    """Write the proxy's own JSON error body."""
    response = JSONResponse({"detail": detail}, status_code=status_code)
    await response(scope, receive, send)


class LLMProxy:
    """ASGI application that forwards `/v1` requests to the LLM upstream."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # A mount only routes HTTP requests here. A websocket has no upstream
        # to talk to, so the proxy drops the connection instead of hanging it.
        if scope["type"] != "http":
            return

        request = Request(scope, receive)
        body = await request.body()
        target = request_path(scope)
        query = scope.get("query_string", b"")
        if query:
            target = f"{target}?{query.decode('latin-1')}"

        try:
            upstream = await self.client.request(
                request.method,
                target,
                content=body,
                headers=_forward_headers(request),
            )
        except httpx.TimeoutException:
            await _send_error(scope, receive, send, HTTP_504_GATEWAY_TIMEOUT, "upstream timeout")
            return
        except httpx.ConnectError:
            await _send_error(scope, receive, send, HTTP_502_BAD_GATEWAY, "upstream unavailable")
            return
        except httpx.HTTPError:
            await _send_error(scope, receive, send, HTTP_502_BAD_GATEWAY, "upstream failure")
            return

        response = Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=_passthrough_headers(upstream),
        )
        await response(scope, receive, send)
