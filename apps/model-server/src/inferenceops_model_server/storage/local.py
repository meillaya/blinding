"""Local filesystem storage backend for model artifacts (stub)."""

from __future__ import annotations

from pathlib import Path

from inferenceops_model_server.schemas import ModelInfo


class LocalStorageBackend:
    """Resolve model artifacts under a local root directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def get_artifact(self, model_id: str, version: str) -> Path:
        """Return the path to the artifact for ``model_id``/``version``."""
        raise NotImplementedError

    def list_models(self) -> list[ModelInfo]:
        """List metadata for every model under the local root."""
        raise NotImplementedError
