"""Reverse-proxy routes: forward /v1 inference calls to the model server (todo 11).

The gateway is transparent on these paths: it forwards the raw request body and
returns the upstream status/headers/body verbatim — schema validation is owned
by the model server (todo 9), which answers 422/404/504 itself. Gateway
credentials (``X-API-Key``) and hop-by-hop headers are stripped before
forwarding, so internal credentials never leak upstream. An unreachable model
server surfaces as 502 and a slow one as 504 — the gateway never crashes on an
unhealthy upstream.
"""

from __future__ import annotations

from typing import Final

import httpx
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

UPSTREAM_TIMEOUT_S: Final = 30.0

_HOP_BY_HOP: Final = frozenset(
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
_STRIP_REQUEST_HEADERS: Final = _HOP_BY_HOP | {"host", "content-length", "x-api-key"}
_STRIP_RESPONSE_HEADERS: Final = _HOP_BY_HOP | {"content-length", "content-encoding"}


def _forward_headers(request: Request) -> dict[str, str]:
    """Request headers safe to forward: no hop-by-hop, host, or gateway credentials."""
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in _STRIP_REQUEST_HEADERS
    }


def _passthrough_headers(response: httpx.Response) -> dict[str, str]:
    """Response headers safe to pass through (httpx already decoded the body)."""
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _STRIP_RESPONSE_HEADERS
    }


async def proxy_request(
    request: Request, client: httpx.AsyncClient, *, backend: str = "model server"
) -> Response:
    """Forward one request to an upstream and mirror its response.

    ``backend`` names the upstream in the gateway's error bodies so a caller can
    tell a model-server failure from an LLM-backend failure. Shared by the
    model-server routes here and the LLM routes (routes/llm.py).
    """
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    body = await request.body()
    try:
        upstream = await client.request(
            request.method, target, content=body, headers=_forward_headers(request)
        )
    except httpx.TimeoutException:
        return JSONResponse({"detail": f"{backend} timeout"}, status_code=504)
    except httpx.HTTPError:
        return JSONResponse({"detail": f"{backend} unavailable"}, status_code=502)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_passthrough_headers(upstream),
    )


def build_proxy_router(client: httpx.AsyncClient, *, include_models: bool = True) -> APIRouter:
    """The proxied model-server routes: /v1/predict, /v1/batch, and /v1/models.

    ``include_models`` is False when an LLM backend is configured: the OpenAI
    ``/v1/models`` surface then belongs to the LLM router (routes/llm.py), so
    exactly one ``/v1/models`` route is ever registered.
    """
    router = APIRouter()

    async def proxy_predict(request: Request) -> Response:
        return await proxy_request(request, client)

    async def proxy_batch(request: Request) -> Response:
        return await proxy_request(request, client)

    async def proxy_models(request: Request) -> Response:
        return await proxy_request(request, client)

    router.add_api_route("/v1/predict", proxy_predict, methods=["POST"])
    router.add_api_route("/v1/batch", proxy_batch, methods=["POST"])
    if include_models:
        router.add_api_route("/v1/models", proxy_models, methods=["GET"])
    return router
