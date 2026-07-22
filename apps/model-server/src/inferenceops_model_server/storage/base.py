"""Storage interface shared by the runtime loader and the model registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from inferenceops_model_server.schemas import ModelInfo


class ModelStorage(ABC):
    """Locate and list model artifacts.

    The single interface both the runtime model loader (Wave 2) and the model
    registry (Wave 6, which adds an S3 backend) program against. Backends
    subclass it so a forgotten method fails at construction, not at first use.
    """

    @abstractmethod
    def get_artifact(self, model_id: str, version: str) -> Path:
        """Return the path to the loadable artifact (``model.pkl``).

        Raises:
            ModelNotFound: when ``model_id``/``version`` is not registered.
        """

    @abstractmethod
    def list_models(self) -> list[ModelInfo]:
        """List metadata for every registered model, in a stable order."""
