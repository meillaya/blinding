"""Reserved LLM routes — placeholders until the CPU-LLM backend lands (todo 43).

``/v1/completions`` and ``/v1/chat/completions`` are part of the gateway's
public surface from day one so clients can already target them, but they
answer 501 until todo 43 wires them to ``LLM_BACKEND`` (config.py). Todo 43
bolts on by replacing this module's 501 handlers behind the same router
factory — the route table (``routes/__init__.py``) does not change.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter
from starlette.responses import JSONResponse

RESERVED_DETAIL: Final = "LLM routes are reserved; implemented in todo 43 (CPU-LLM backend)"


def build_llm_router() -> APIRouter:
    """The reserved OpenAI-compatible LLM routes (501 until todo 43)."""
    router = APIRouter()

    async def completions_reserved() -> JSONResponse:
        return JSONResponse({"detail": RESERVED_DETAIL}, status_code=501)

    async def chat_completions_reserved() -> JSONResponse:
        return JSONResponse({"detail": RESERVED_DETAIL}, status_code=501)

    router.add_api_route("/v1/completions", completions_reserved, methods=["POST"])
    router.add_api_route("/v1/chat/completions", chat_completions_reserved, methods=["POST"])
    return router
