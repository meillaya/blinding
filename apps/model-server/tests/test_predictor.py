"""Predictor tests against the real todo-6 artifact.

Deterministic by construction: a seeded model plus a fixed 20-feature row give
identical output on every run, so re-runs pass without pinning sklearn floats.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from inferenceops_model_server import model_loader
from inferenceops_model_server.errors import FeatureCountMismatch
from inferenceops_model_server.model_loader import TrainedModel
from inferenceops_model_server.predictor import predict
from inferenceops_model_server.storage.local import LocalStorageBackend

MODEL_ID: Final = "fraud-detector"
MODEL_VERSION: Final = "1.0.0"
FEATURE_COUNT: Final = 20
_ARTIFACTS_ROOT: Final = Path(__file__).resolve().parents[3] / "models" / "artifacts"
SAMPLE_ROW: Final = tuple(float(i) * 0.1 for i in range(FEATURE_COUNT))


@pytest.fixture(scope="session")
def trained_model() -> TrainedModel:
    """The real todo-6 classifier, loaded once for the whole test session."""
    return model_loader.load_model(
        LocalStorageBackend(_ARTIFACTS_ROOT), MODEL_ID, MODEL_VERSION
    )


def test_predict_returns_label_and_valid_score(trained_model: TrainedModel) -> None:
    result = predict(trained_model, list(SAMPLE_ROW))
    assert result.label in {0, 1}
    assert 0.0 <= result.score <= 1.0


def test_predict_is_deterministic(trained_model: TrainedModel) -> None:
    first = predict(trained_model, list(SAMPLE_ROW))
    second = predict(trained_model, list(SAMPLE_ROW))
    assert first == second


@pytest.mark.parametrize("count", [0, FEATURE_COUNT - 1, FEATURE_COUNT + 1])
def test_predict_rejects_wrong_feature_count(
    trained_model: TrainedModel, count: int
) -> None:
    with pytest.raises(FeatureCountMismatch) as excinfo:
        predict(trained_model, [0.0] * count)
    assert excinfo.value.expected == FEATURE_COUNT
    assert excinfo.value.actual == count
