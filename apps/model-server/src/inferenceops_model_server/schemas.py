"""Request/response schemas for the model server API (stub)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PredictRequest(BaseModel):
    """A single prediction request."""

    model_config = ConfigDict(frozen=True)

    features: list[float]


class PredictResponse(BaseModel):
    """A single prediction response."""

    model_config = ConfigDict(frozen=True)

    prediction: int
    score: float
    model_id: str
    model_version: str


class BatchRequest(BaseModel):
    """A batch of prediction requests."""

    model_config = ConfigDict(frozen=True)

    requests: list[PredictRequest]


class BatchResponse(BaseModel):
    """A batch of prediction responses."""

    model_config = ConfigDict(frozen=True)

    responses: list[PredictResponse]


class ModelInfo(BaseModel):
    """Metadata describing a registered model artifact."""

    model_config = ConfigDict(frozen=True)

    model_id: str
    version: str


class ModelNotFound(Exception):
    """Raised when a requested model artifact does not exist."""

    def __init__(self, model_id: str, version: str) -> None:
        self.model_id = model_id
        self.version = version
        super().__init__(f"model {model_id!r} version {version!r} not found")
