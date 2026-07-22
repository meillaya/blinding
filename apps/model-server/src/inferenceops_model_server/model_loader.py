"""Model artifact loading and caching (stub)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from inferenceops_model_server.storage.base import ModelStorage


class TrainedModel(Protocol):
    """A loaded model capable of scoring feature rows."""

    def predict(self, features: list[list[float]]) -> list[int]:
        """Predict class labels for each feature row."""
        ...


def load_model(storage: ModelStorage, model_id: str, version: str) -> TrainedModel:
    """Load and cache the model artifact identified by ``model_id``/``version``."""
    raise NotImplementedError
