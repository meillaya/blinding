"""Request authentication and API-key validation (stub)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller identity."""

    api_key_id: str
    tenant: str


def authenticate(api_key: str) -> Principal:
    """Validate ``api_key`` and return the authenticated principal."""
    raise NotImplementedError
