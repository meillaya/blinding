"""Storage interface shared by the runtime loader and the model registry (stub)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from inferenceops_model_server.schemas import ModelInfo


class ModelStorage(Protocol):
    """Capability to locate and list model artifacts."""

    def get_artifact(self, model_id: str, version: str) -> Path:
        """Return the filesystem path to the artifact for ``model_id``/``version``."""
        ...

    def list_models(self) -> list[ModelInfo]:
        """List metadata for every registered model."""
        ...
