"""OpenAI-compatible LLM routes — proxy to the configured LLM backend (todo 43).

``/v1/completions`` and ``/v1/chat/completions`` (and ``/v1/models`` when an LLM
is configured) forward to the backend selected by ``LLM_BACKEND``/``LLM_URL``
(config.py): ``llama-cpu`` (llama.cpp), ``ollama``, or ``vllm``. The gateway is
transparent here exactly as on the model-server routes — the LLM backend owns
schema validation and answers 4xx itself; an unreachable backend surfaces as 502
and a slow one as 504. With no backend configured the routes answer 503 so a
gateway without an LLM fails loud, not silent.
"""

from __future__ import annotations

from typing import Final

import httpx
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from inferenceops_gateway.routes.proxy import proxy_request

# CPU LLM generation is slow; give the LLM upstream a longer leash than the
# model-server proxy (proxy.UPSTREAM_TIMEOUT_S) so a small completion is not
# cut off. main.py builds the LLM client with this timeout.
LLM_TIMEOUT_S: Final = 120.0

_BACKEND_LABEL: Final = "LLM backend"
_NOT_CONFIGURED: Final = "LLM backend not configured"


async def _llm_proxy(request: Request, client: httpx.AsyncClient | None) -> Response:
    """Proxy one OpenAI-compatible request, or 503 when no backend is wired."""
    if client is None:
        return JSONResponse({"detail": _NOT_CONFIGURED}, status_code=503)
    return await proxy_request(request, client, backend=_BACKEND_LABEL)


def build_llm_router(client: httpx.AsyncClient | None) -> APIRouter:
    """The OpenAI-compatible LLM routes over the given backend client.

    ``/v1/models`` is registered here ONLY when a backend is configured; with no
    backend it stays on the model-server proxy (routes/proxy.py), so the route is
    always served by exactly one router.
    """
    router = APIRouter()

    async def completions(request: Request) -> Response:
        return await _llm_proxy(request, client)

    async def chat_completions(request: Request) -> Response:
        return await _llm_proxy(request, client)

    async def models(request: Request) -> Response:
        return await _llm_proxy(request, client)

    router.add_api_route("/v1/completions", completions, methods=["POST"])
    router.add_api_route("/v1/chat/completions", chat_completions, methods=["POST"])
    if client is not None:
        router.add_api_route("/v1/models", models, methods=["GET"])
    return router
