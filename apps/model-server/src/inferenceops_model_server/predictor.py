"""Prediction wrapper over a loaded model (stub)."""

from __future__ import annotations

from dataclasses import dataclass

from inferenceops_model_server.model_loader import TrainedModel


@dataclass(frozen=True, slots=True)
class Prediction:
    """A single prediction result."""

    label: int
    score: float


def predict(model: TrainedModel, features: list[float]) -> Prediction:
    """Run ``model`` on ``features`` and return the prediction."""
    raise NotImplementedError
