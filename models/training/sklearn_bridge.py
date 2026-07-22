# pyright: reportMissingTypeStubs=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Typed seam over scikit-learn and joblib.

scikit-learn and joblib ship no ``py.typed`` marker and publish no type
stubs, so basedpyright sees every symbol imported from them as partially
``Unknown``. This module is the single boundary where those untyped calls
happen; every value crossing out of it carries an explicit, fully known
annotation, so the rest of the training pipeline type-checks under
``strict`` with zero suppressions. The file-level suppressions above cover
only the untyped-library seam itself and cannot leak into callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import joblib
import numpy as np
from numpy.typing import NDArray
from sklearn.datasets import make_classification
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from sklearn.model_selection import train_test_split

if TYPE_CHECKING:
    from dataset import DatasetConfig, Hyperparameters

FraudSplits = tuple[
    NDArray[np.float64],
    NDArray[np.float64],
    NDArray[np.int64],
    NDArray[np.int64],
]


def _as_float_matrix(value: object) -> NDArray[np.float64]:
    if not isinstance(value, np.ndarray):
        msg = f"scikit-learn returned {type(value).__name__}; expected ndarray"
        raise TypeError(msg)
    return value


def _as_label_vector(value: object) -> NDArray[np.int64]:
    if not isinstance(value, np.ndarray):
        msg = f"scikit-learn returned {type(value).__name__}; expected ndarray"
        raise TypeError(msg)
    return value


def generate_fraud_splits(spec: DatasetConfig, *, random_state: int) -> FraudSplits:
    """Generate a deterministic imbalanced fraud-like dataset, split train/val."""
    features, labels = make_classification(
        n_samples=spec.n_samples,
        n_features=spec.n_features,
        n_informative=spec.n_informative,
        n_redundant=spec.n_redundant,
        weights=[1.0 - spec.fraud_ratio, spec.fraud_ratio],
        class_sep=spec.class_sep,
        flip_y=spec.flip_y,
        random_state=random_state,
    )
    parts = train_test_split(
        features,
        labels,
        test_size=spec.val_fraction,
        stratify=labels,
        random_state=random_state,
    )
    return (
        _as_float_matrix(parts[0]),
        _as_float_matrix(parts[1]),
        _as_label_vector(parts[2]),
        _as_label_vector(parts[3]),
    )


def fit_fraud_classifier(
    x_train: NDArray[np.float64],
    y_train: NDArray[np.int64],
    hyperparameters: Hyperparameters,
    *,
    random_state: int,
) -> GradientBoostingClassifier:
    # Single-threaded GradientBoostingClassifier (not the histogram variant)
    # so the fitted model is bit-for-bit reproducible across runs.
    classifier = GradientBoostingClassifier(
        n_estimators=hyperparameters.n_estimators,
        max_depth=hyperparameters.max_depth,
        learning_rate=hyperparameters.learning_rate,
        random_state=random_state,
    )
    classifier.fit(x_train, y_train)
    return classifier


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


def dump_model(model: GradientBoostingClassifier, path: Path) -> None:
    joblib.dump(model, path)


def load_model(path: Path) -> GradientBoostingClassifier:
    loaded: GradientBoostingClassifier = joblib.load(path)
    return loaded
