"""Typed errors for the model server.

Every error carries its context as typed fields so handlers (the FastAPI
server, todo 9) can build precise responses without parsing message strings.
"""

from __future__ import annotations


class ModelNotFound(Exception):
    """Raised when a requested model artifact does not exist in storage."""

    def __init__(self, model_id: str, version: str) -> None:
        self.model_id = model_id
        self.version = version
        super().__init__(f"model {model_id!r} version {version!r} not found")


class FeatureCountMismatch(Exception):
    """Raised when a feature row's length does not match the model's arity."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"model expects {expected} features, got {actual}")
