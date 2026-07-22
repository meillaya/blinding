"""Gateway route table (todo 11).

``build_route_table`` is the single place the gateway's /v1 surface is
assembled: the model-server proxy routes today, plus the reserved LLM routes
that todo 43 flips from 501 to real. Adding a backend means adding one router
here — middleware and app wiring stay untouched.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter

from inferenceops_gateway.routes.llm import build_llm_router
from inferenceops_gateway.routes.proxy import build_proxy_router


def build_route_table(client: httpx.AsyncClient) -> APIRouter:
    """Assemble every gateway route onto one router."""
    table = APIRouter()
    table.include_router(build_proxy_router(client))
    table.include_router(build_llm_router())
    return table
