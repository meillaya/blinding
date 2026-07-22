"""Prediction wrapper over a loaded model."""

from __future__ import annotations

from dataclasses import dataclass

from inferenceops_model_server._sklearn_io import positive_class_score, predict_label
from inferenceops_model_server.errors import FeatureCountMismatch
from inferenceops_model_server.model_loader import TrainedModel


@dataclass(frozen=True, slots=True)
class Prediction:
    """A single prediction: class label + positive-class probability."""

    label: int
    score: float


def predict(model: TrainedModel, features: list[float]) -> Prediction:
    """Score one feature row with ``model``.

    ``label`` is the predicted class (the server maps it to
    ``PredictResponse.prediction``); ``score`` is the positive-class
    probability (``predict_proba[:, 1]``).

    Raises:
        FeatureCountMismatch: when ``len(features)`` != ``model.n_features_in_``.
    """
    if len(features) != model.n_features_in_:
        raise FeatureCountMismatch(expected=model.n_features_in_, actual=len(features))
    return Prediction(
        label=predict_label(model, features),
        score=positive_class_score(model, features),
    )
