"""Schema tests: valid input accepted, malformed input rejected at the boundary.

Malformed payloads go through ``model_validate`` (the JSON-boundary entry
point FastAPI uses) so the wrong-shaped data is a runtime input, not a static
type error in the test itself.
"""

from __future__ import annotations

import math
from typing import Final

import pytest
from inferenceops_model_server import schemas
from inferenceops_model_server.errors import ModelNotFound
from inferenceops_model_server.schemas import (
    BatchRequest,
    BatchResponse,
    ModelInfo,
    PredictRequest,
    PredictResponse,
)
from pydantic import ValidationError

VALID_ROW: Final = [float(i) for i in range(20)]


def test_predict_request_accepts_valid_features() -> None:
    assert PredictRequest(features=VALID_ROW).features == VALID_ROW


def test_predict_request_coerces_integral_features_to_float() -> None:
    assert PredictRequest(features=[1, 2, 3]).features == [1.0, 2.0, 3.0]


def test_predict_request_rejects_non_numeric_features() -> None:
    with pytest.raises(ValidationError):
        PredictRequest.model_validate({"features": ["definitely-not-a-float"]})


def test_predict_request_rejects_non_list_features() -> None:
    with pytest.raises(ValidationError):
        PredictRequest.model_validate({"features": "not-a-list"})


def test_predict_request_rejects_empty_features() -> None:
    with pytest.raises(ValidationError):
        PredictRequest(features=[])


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_predict_request_rejects_non_finite_features(value: float) -> None:
    with pytest.raises(ValidationError):
        PredictRequest(features=[value])


def test_predict_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PredictRequest.model_validate({"features": VALID_ROW, "surprise": 1})


def test_predict_request_is_immutable() -> None:
    request = PredictRequest(features=VALID_ROW)
    with pytest.raises(ValidationError):
        request.features = [0.0]


def test_predict_response_roundtrip() -> None:
    response = PredictResponse(
        prediction=1, score=0.87, model_id="fraud-detector", model_version="1.0.0"
    )
    assert response.model_dump() == {
        "prediction": 1,
        "score": 0.87,
        "model_id": "fraud-detector",
        "model_version": "1.0.0",
    }


@pytest.mark.parametrize("score", [-0.1, 1.1])
def test_predict_response_rejects_out_of_range_score(score: float) -> None:
    with pytest.raises(ValidationError):
        PredictResponse(prediction=1, score=score, model_id="m", model_version="v")


def test_batch_request_requires_at_least_one_request() -> None:
    with pytest.raises(ValidationError):
        BatchRequest(requests=[])


def test_batch_request_nests_predict_requests() -> None:
    batch = BatchRequest(
        requests=[PredictRequest(features=VALID_ROW), PredictRequest(features=[1.0])]
    )
    assert len(batch.requests) == 2
    assert batch.requests[0].features == VALID_ROW


def test_batch_response_roundtrip() -> None:
    response = BatchResponse(
        responses=[
            PredictResponse(prediction=0, score=0.1, model_id="m", model_version="v")
        ]
    )
    assert BatchResponse.model_validate(response.model_dump()) == response


def test_model_info_requires_model_id_and_version() -> None:
    info = ModelInfo(model_id="fraud-detector", version="1.0.0")
    assert info.model_id == "fraud-detector"
    assert info.version == "1.0.0"
    with pytest.raises(ValidationError):
        ModelInfo.model_validate({"model_id": "x"})


def test_model_info_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ModelInfo.model_validate({"model_id": "x", "version": "1", "extra": True})


def test_model_not_found_is_reexported_from_schemas() -> None:
    assert schemas.ModelNotFound is ModelNotFound


def test_model_not_found_carries_typed_context() -> None:
    error = ModelNotFound("fraud-detector", "9.9.9")
    assert error.model_id == "fraud-detector"
    assert error.version == "9.9.9"
    assert "fraud-detector" in str(error)
    assert "9.9.9" in str(error)


def test_feature_count_mismatch_carries_typed_context() -> None:
    error = schemas.FeatureCountMismatch(expected=20, actual=19)
    assert error.expected == 20
    assert error.actual == 19
    assert "20" in str(error)
    assert "19" in str(error)
