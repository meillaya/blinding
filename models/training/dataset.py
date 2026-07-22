"""Deterministic fraud-like dataset generation and training configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self

import numpy as np
import yaml
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sklearn_bridge import generate_fraud_splits

REPO_ROOT = Path(__file__).resolve().parents[2]


class TrainingError(Exception):
    """Raised when training or evaluation cannot proceed."""


class DatasetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_samples: int = Field(gt=0)
    n_features: int = Field(gt=0)
    n_informative: int = Field(gt=0)
    n_redundant: int = Field(ge=0)
    class_sep: float = Field(gt=0)
    flip_y: float = Field(ge=0, le=1)
    fraud_ratio: float = Field(gt=0, lt=1)
    val_fraction: float = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def feature_budget_fits(self) -> Self:
        if self.n_informative + self.n_redundant > self.n_features:
            msg = "n_informative + n_redundant must not exceed n_features"
            raise ValueError(msg)
        return self


class Hyperparameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_estimators: int = Field(gt=0)
    max_depth: int = Field(gt=0)
    learning_rate: float = Field(gt=0)


class TrainingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    seed: int
    dataset: DatasetConfig
    hyperparameters: Hyperparameters
    artifact_dir: Path


def load_config(path: Path) -> TrainingConfig:
    """Parse and validate the YAML training config at the boundary."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrainingError(f"cannot read training config {path}: {exc}") from exc
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TrainingError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return TrainingConfig.model_validate(parsed)
    except ValidationError as exc:
        raise TrainingError(f"invalid training config {path}: {exc}") from exc


def resolve_artifact_dir(configured: Path) -> Path:
    return configured if configured.is_absolute() else REPO_ROOT / configured


@dataclass(frozen=True, slots=True)
class DatasetSplits:
    x_train: NDArray[np.float64]
    x_val: NDArray[np.float64]
    y_train: NDArray[np.int64]
    y_val: NDArray[np.int64]
    feature_names: tuple[str, ...]


def build_dataset(config: TrainingConfig) -> DatasetSplits:
    """Build the seeded train/val splits described by ``config``."""
    spec = config.dataset
    x_train, x_val, y_train, y_val = generate_fraud_splits(
        spec,
        random_state=config.seed,
    )
    feature_names = tuple(f"feature_{i:02d}" for i in range(spec.n_features))
    return DatasetSplits(x_train, x_val, y_train, y_val, feature_names)
