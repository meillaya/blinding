# pyright: reportMissingTypeStubs=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Typed seam over joblib and scikit-learn for the model-server runtime.

joblib and scikit-learn publish no ``py.typed`` marker and no type stubs, so
basedpyright sees every symbol imported from them as partially ``Unknown``.
This module is the single runtime boundary where those untyped calls happen;
every value crossing out of it carries an explicit, fully known annotation, so
``model_loader`` and ``predictor`` type-check under strict with zero
suppressions. The file-level suppressions above cover only the untyped-library
seam itself and cannot leak into callers. Mirrors ``models/training/
sklearn_bridge.py`` on the training side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import joblib
import numpy as np
from numpy.typing import NDArray


class TrainedModel(Protocol):
    """The scoring surface a fitted classifier exposes to the server."""

    n_features_in_: int

    def predict(self, features: NDArray[np.float64]) -> NDArray[np.int64]:
        """Predict class labels for each row of ``features``."""
        ...

    def predict_proba(self, features: NDArray[np.float64]) -> NDArray[np.float64]:
        """Per-class probabilities for each row of ``features``."""
        ...


def load_classifier(path: Path) -> TrainedModel:
    """Load the joblib-pickled classifier at ``path``."""
    loaded: TrainedModel = joblib.load(path)
    return loaded


def _single_row(features: list[float]) -> NDArray[np.float64]:
    """Reshape one feature row into the ``(1, n)`` matrix sklearn expects."""
    row: NDArray[np.float64] = np.asarray(features, dtype=np.float64).reshape(1, -1)
    return row


def predict_label(model: TrainedModel, features: list[float]) -> int:
    """Class label for one feature row."""
    labels = model.predict(_single_row(features))
    return int(labels[0])


def positive_class_score(model: TrainedModel, features: list[float]) -> float:
    """Positive-class probability (``predict_proba[:, 1]``) for one feature row."""
    probabilities = model.predict_proba(_single_row(features))
    return float(probabilities[0, 1])
