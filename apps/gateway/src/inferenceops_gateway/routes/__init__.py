"""Gateway route table (todo 11; LLM routes wired in todo 43).

``build_route_table`` is the single place the gateway's /v1 surface is
assembled: the model-server proxy routes plus the OpenAI-compatible LLM routes.
``llm_client`` is None when no LLM backend is configured — the LLM routes then
answer 503 and ``/v1/models`` stays on the model-server proxy; with a backend it
moves to the LLM router. Adding a backend means adding one router here —
middleware and app wiring stay untouched.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter

from inferenceops_gateway.routes.llm import build_llm_router
from inferenceops_gateway.routes.proxy import build_proxy_router


def build_route_table(
    client: httpx.AsyncClient, llm_client: httpx.AsyncClient | None
) -> APIRouter:
    """Assemble every gateway route onto one router."""
    table = APIRouter()
    table.include_router(build_proxy_router(client, include_models=llm_client is None))
    table.include_router(build_llm_router(llm_client))
    return table
