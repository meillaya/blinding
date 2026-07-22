"""Request/response schemas for the model server API.

Structural validation lives here: finite floats, non-empty feature rows and
batches, probability bounds. Arity against the loaded model (e.g. exactly 20
features) is enforced by ``predictor.predict``, which is the layer that knows
the model's ``n_features_in_``. Typed errors are re-exported from
``inferenceops_model_server.errors`` for API-layer convenience.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AllowInfNan, BaseModel, ConfigDict, Field

from inferenceops_model_server.errors import FeatureCountMismatch as FeatureCountMismatch
from inferenceops_model_server.errors import ModelNotFound as ModelNotFound

type FiniteFloat = Annotated[float, AllowInfNan(False)]


class PredictRequest(BaseModel):
    """A single prediction request: one non-empty row of finite features."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    features: list[FiniteFloat] = Field(min_length=1)


class PredictResponse(BaseModel):
    """A single prediction response."""

    model_config = ConfigDict(frozen=True)

    prediction: int
    score: float = Field(ge=0.0, le=1.0)
    model_id: str
    model_version: str


class BatchRequest(BaseModel):
    """A batch of one or more prediction requests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requests: list[PredictRequest] = Field(min_length=1)


class BatchResponse(BaseModel):
    """A batch of prediction responses."""

    model_config = ConfigDict(frozen=True)

    responses: list[PredictResponse]


class ModelInfo(BaseModel):
    """Metadata describing a registered model artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    version: str
