"""Gateway entrypoint: builds the FastAPI application (stub)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app() -> FastAPI:
    """Construct the gateway FastAPI application."""
    raise NotImplementedError
