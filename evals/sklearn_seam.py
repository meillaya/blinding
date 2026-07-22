# pyright: reportMissingTypeStubs=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Typed seam over scikit-learn and joblib for the evaluation gates.

Mirrors ``models/training/sklearn_bridge.py``: scikit-learn and joblib ship no
``py.typed`` marker and publish no type stubs, so basedpyright sees every
symbol imported from them as partially ``Unknown``. This module is the single
boundary where those untyped calls happen for the eval scripts; every value
crossing out of it carries an explicit, fully known annotation, so the gate
scripts type-check under ``strict`` with zero suppressions. The file-level
suppressions above cover only the untyped-library seam itself and cannot leak
into callers.
"""

from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
from numpy.typing import NDArray
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score


def _as_label_vector(value: object) -> NDArray[np.int64]:
    if not isinstance(value, np.ndarray):
        msg = f"scikit-learn returned {type(value).__name__}; expected ndarray"
        raise TypeError(msg)
    return value


def load_model(path: Path) -> GradientBoostingClassifier:
    loaded: GradientBoostingClassifier = joblib.load(path)
    return loaded


def predict_labels(
    model: GradientBoostingClassifier,
    features: NDArray[np.float64],
) -> NDArray[np.int64]:
    return _as_label_vector(model.predict(features))


def positive_class_scores(
    model: GradientBoostingClassifier,
    features: NDArray[np.float64],
) -> NDArray[np.float64]:
    probabilities = model.predict_proba(features)
    scores: NDArray[np.float64] = probabilities[:, 1]
    return scores


def accuracy(y_true: NDArray[np.int64], y_pred: NDArray[np.int64]) -> float:
    return float(accuracy_score(y_true, y_pred))


def f1(y_true: NDArray[np.int64], y_pred: NDArray[np.int64]) -> float:
    return float(f1_score(y_true, y_pred))


def roc_auc(y_true: NDArray[np.int64], scores: NDArray[np.float64]) -> float:
    return float(roc_auc_score(y_true, scores))
