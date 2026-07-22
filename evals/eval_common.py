"""Shared contract for the evaluation gates: paths, golden dataset, reports.

Golden dataset provenance
-------------------------
``evals/golden_dataset.jsonl`` is the held-out validation split of the SAME
seeded data-generating process that produced the trained model (todo 6,
``models/training/config.yaml``: ``make_classification`` with ``seed=42``,
``n_features=20``, ``n_informative=10``, ``n_redundant=5``,
``weights=[0.95, 0.05]``, ``class_sep=1.2``, ``flip_y=0.01``, then a
stratified 25% ``train_test_split``). Sharing the seed is REQUIRED:
``make_classification`` shuffles feature columns per seed, so a fresh seed
would scramble column semantics and make the labels incoherent with the
trained model. Each row's ``expected`` label is exactly the generator label
for that row (2000 rows, ~5.5% fraud). Feature values are rounded to 6
decimals for readability; verified that the rounding does not change a single
prediction of the trained model (accuracy 0.9655 before and after).

Regenerate byte-identically from the repo root:

    uv run python - <<'PY'
    import json, sys
    sys.path.insert(0, "models/training")
    from dataset import build_dataset, load_config
    from pathlib import Path

    splits = build_dataset(load_config(Path("models/training/config.yaml")))
    with open("evals/golden_dataset.jsonl", "w", encoding="utf-8") as fh:
        for features, label in zip(splits.x_val, splits.y_val):
            row = {"features": [round(float(v), 6) for v in features],
                   "expected": int(label)}
            fh.write(json.dumps(row) + "\n")
    PY
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sklearn_seam import GradientBoostingClassifier
from sklearn_seam import load_model as load_model_artifact

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = REPO_ROOT / "models" / "artifacts" / "fraud_detector" / "model.pkl"
GOLDEN_DATASET_PATH = REPO_ROOT / "evals" / "golden_dataset.jsonl"
REPORTS_DIR = REPO_ROOT / "evals" / "reports"
BASELINE_PATH = REPORTS_DIR / "baseline.json"

# Must match feature_count in models/artifacts/fraud_detector/model_meta.json.
FEATURE_COUNT = 20


class EvalError(Exception):
    """Raised when an evaluation gate cannot proceed."""


def load_trained_model(path: Path = MODEL_PATH) -> GradientBoostingClassifier:
    """Load the trained artifact, with a clear error when it is missing."""
    try:
        return load_model_artifact(path)
    except OSError as exc:
        msg = f"cannot load model artifact {path}; run `make train` first: {exc}"
        raise EvalError(msg) from exc


@dataclass(frozen=True, slots=True)
class GoldenDataset:
    features: NDArray[np.float64]
    labels: NDArray[np.int64]

    @property
    def n_rows(self) -> int:
        return int(self.features.shape[0])


class GoldenRow(BaseModel):
    """One golden case: exactly FEATURE_COUNT numeric features + a 0/1 label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    features: list[float] = Field(min_length=FEATURE_COUNT, max_length=FEATURE_COUNT)
    expected: Literal[0, 1]


def _validation_details(exc: ValidationError) -> str:
    return "; ".join(f"{error['loc']}: {error['msg']}" for error in exc.errors())


def _parse_row(path: Path, line_number: int, line: str) -> GoldenRow:
    """Parse one golden JSONL row into a typed GoldenRow, or raise EvalError."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise EvalError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    try:
        return GoldenRow.model_validate(raw)
    except ValidationError as exc:
        raise EvalError(f"{path}:{line_number}: {_validation_details(exc)}") from exc


def load_golden_dataset(path: Path = GOLDEN_DATASET_PATH) -> GoldenDataset:
    """Parse the golden JSONL into typed arrays, rejecting malformed rows."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"cannot read golden dataset {path}: {exc}") from exc
    feature_rows: list[list[float]] = []
    label_rows: list[int] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        row = _parse_row(path, line_number, line)
        feature_rows.append(row.features)
        label_rows.append(row.expected)
    if not feature_rows:
        raise EvalError(f"golden dataset {path} contains no rows")
    return GoldenDataset(
        features=np.asarray(feature_rows, dtype=np.float64),
        labels=np.asarray(label_rows, dtype=np.int64),
    )


class Metrics(BaseModel):
    """Gated metrics; also the parse schema for eval/baseline reports."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    accuracy: float
    f1: float
    roc_auc: float


def write_report(report: dict[str, object], path: Path) -> None:
    """Write a JSON report for gate consumption."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"cannot write report {path}: {exc}") from exc


def read_metrics(path: Path) -> Metrics:
    """Parse accuracy/f1/roc_auc from an eval or baseline report."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"cannot read metrics report {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvalError(f"invalid JSON in {path}: {exc}") from exc
    try:
        return Metrics.model_validate(parsed)
    except ValidationError as exc:
        raise EvalError(f"{path}: {_validation_details(exc)}") from exc


def latest_eval_report(reports_dir: Path = REPORTS_DIR) -> Path:
    """Return the newest eval_*.json report (UTC timestamps sort lexically)."""
    candidates = sorted(reports_dir.glob("eval_*.json"))
    if not candidates:
        raise EvalError(f"no eval_*.json report in {reports_dir}; run run_eval.py first")
    return candidates[-1]
