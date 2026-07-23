"""Offline data-drift detection job (spec §M7, todo 35).

Compares a CURRENT feature distribution (a JSONL file of ``{"features": [...]}``
rows — e.g. recent traffic exported offline) against a BASELINE (the golden
dataset / training distribution, ``evals/golden_dataset.jsonl``) and reduces the
per-feature shift to a single drift score:

- PSI (Population Stability Index) per feature: the baseline column is split
  into decile bins and ``PSI = Σ (current_share - baseline_share) *
  ln(current_share / baseline_share)``; ~0 in-distribution, >0.25 significant.
- KS (two-sample Kolmogorov–Smirnov) per feature: ``max |CDF_baseline -
  CDF_current|``, reported alongside as a rank-free confirmation of the shift.

The drift score is the MEAN per-feature PSI. The job sets
``inferenceops_drift_score`` on a dedicated ``MetricsRegistry`` (reusing the
model-server's canonical metric definitions, todo 8/35), optionally renders the
exposition to a textfile-collector file, and alerts on stderr when the score
exceeds the threshold. Runs fully offline — no online server required::

    PYTHONPATH=apps/worker/src:apps/model-server/src uv run python \\
        -m inferenceops_worker.drift_job \\
        --baseline evals/golden_dataset.jsonl --current recent.jsonl

Exit codes: 0 = stable, 1 = drift alert, 2 = operational error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
from inferenceops_model_server.metrics import MetricsRegistry
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_DRIFT_THRESHOLD: Final = 0.2
_PSI_BINS: Final = 10
_EPSILON: Final = 1e-6


class DriftJobError(Exception):
    """Raised when the drift job cannot load its inputs or score them."""


class _FeatureRow(BaseModel):
    """Boundary parse for one JSONL row; extra keys (e.g. ``expected``) ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    features: list[float] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class FeatureDrift:
    """One feature column's shift: PSI (the score input) + KS (confirmation)."""

    index: int
    psi: float
    ks: float


@dataclass(frozen=True, slots=True)
class DriftResult:
    """Outcome of a drift-detection run."""

    drifted: bool
    score: float
    threshold: float
    features: tuple[FeatureDrift, ...]


@dataclass(frozen=True, slots=True)
class DriftJobConfig:
    """Inputs for one offline drift-detection run."""

    baseline_path: Path
    current_path: Path
    threshold: float = DEFAULT_DRIFT_THRESHOLD
    metrics_out_path: Path | None = None


def load_features(path: Path) -> NDArray[np.float64]:
    """Parse a JSONL file of ``{"features": [...]}`` rows into a 2-D matrix."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DriftJobError(f"cannot read dataset {path}: {exc}") from exc
    rows: list[list[float]] = []
    width: int | None = None
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = _FeatureRow.model_validate(json.loads(line))
        except (ValidationError, json.JSONDecodeError) as exc:
            raise DriftJobError(f"{path}:{line_number}: invalid row: {exc}") from exc
        if width is None:
            width = len(parsed.features)
        elif len(parsed.features) != width:
            raise DriftJobError(
                f"{path}:{line_number}: expected {width} features, got {len(parsed.features)}"
            )
        rows.append(parsed.features)
    if not rows:
        raise DriftJobError(f"dataset {path} contains no rows")
    return np.asarray(rows, dtype=np.float64)


def population_stability_index(
    baseline: NDArray[np.float64], current: NDArray[np.float64], bins: int = _PSI_BINS
) -> float:
    """PSI of one feature column: ~0 in-distribution, >0.25 a significant shift."""
    edges = np.quantile(baseline, np.linspace(0.0, 1.0, bins + 1))
    edges[0] = -np.inf
    edges[-1] = np.inf
    edges = np.unique(edges)
    baseline_share = np.clip(np.histogram(baseline, bins=edges)[0] / baseline.size, _EPSILON, None)
    current_share = np.clip(np.histogram(current, bins=edges)[0] / current.size, _EPSILON, None)
    return float(np.sum((current_share - baseline_share) * np.log(current_share / baseline_share)))


def kolmogorov_smirnov_statistic(
    baseline: NDArray[np.float64], current: NDArray[np.float64]
) -> float:
    """Two-sample KS statistic of one feature column: max |CDF difference|."""
    baseline_sorted = np.sort(baseline)
    current_sorted = np.sort(current)
    points = np.unique(np.concatenate((baseline_sorted, current_sorted)))
    baseline_cdf = np.searchsorted(baseline_sorted, points, side="right") / baseline.size
    current_cdf = np.searchsorted(current_sorted, points, side="right") / current.size
    return float(np.max(np.abs(baseline_cdf - current_cdf)))


def detect_drift(
    baseline: NDArray[np.float64],
    current: NDArray[np.float64],
    threshold: float = DEFAULT_DRIFT_THRESHOLD,
) -> DriftResult:
    """Score the shift of ``current`` vs ``baseline``; drifted when mean PSI > threshold."""
    if baseline.ndim != 2 or current.ndim != 2:
        raise DriftJobError("baseline and current must be 2-D feature matrices")
    if baseline.shape[0] == 0 or current.shape[0] == 0:
        raise DriftJobError("baseline and current must each contain at least one row")
    if baseline.shape[1] != current.shape[1]:
        raise DriftJobError(
            f"feature count mismatch: baseline has {baseline.shape[1]}, "
            f"current has {current.shape[1]}"
        )
    features = tuple(
        FeatureDrift(
            index=column,
            psi=population_stability_index(baseline[:, column], current[:, column]),
            ks=kolmogorov_smirnov_statistic(baseline[:, column], current[:, column]),
        )
        for column in range(baseline.shape[1])
    )
    score = sum(feature.psi for feature in features) / len(features)
    return DriftResult(
        drifted=score > threshold, score=score, threshold=threshold, features=features
    )


def run_drift_job(config: DriftJobConfig) -> DriftResult:
    """Run offline drift detection, publish the score gauge, alert when drifted."""
    baseline = load_features(config.baseline_path)
    current = load_features(config.current_path)
    result = detect_drift(baseline, current, config.threshold)
    registry = MetricsRegistry()
    registry.set_drift_score(result.score)
    if config.metrics_out_path is not None:
        try:
            config.metrics_out_path.parent.mkdir(parents=True, exist_ok=True)
            config.metrics_out_path.write_text(registry.render(), encoding="utf-8")
        except OSError as exc:
            raise DriftJobError(
                f"cannot write metrics file {config.metrics_out_path}: {exc}"
            ) from exc
    if result.drifted:
        print(
            f"DRIFT ALERT: drift score {result.score:.4f} exceeds threshold "
            f"{result.threshold:.4f}",
            file=sys.stderr,
        )
    return result


@dataclass(frozen=True, slots=True)
class _DriftJobArgs:
    """Typed drift-job CLI arguments (parsed once at the boundary)."""

    baseline: Path
    current: Path
    threshold: float
    metrics_out: Path | None


def _parse_args(argv: Sequence[str] | None) -> _DriftJobArgs:
    parser = argparse.ArgumentParser(description="Offline data-drift detection (PSI/KS).")
    parser.add_argument("--baseline", required=True, type=Path, help="Baseline JSONL distribution.")
    parser.add_argument("--current", required=True, type=Path, help="Current JSONL to check.")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_DRIFT_THRESHOLD, help="Mean-PSI alert threshold."
    )
    parser.add_argument(
        "--metrics-out", type=Path, default=None, help="Write the Prometheus exposition here."
    )
    ns = parser.parse_args(argv)
    return _DriftJobArgs(
        baseline=ns.baseline,
        current=ns.current,
        threshold=float(ns.threshold),
        metrics_out=ns.metrics_out,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 = stable, 1 = drift alert, 2 = operational error."""
    args = _parse_args(argv)
    try:
        result = run_drift_job(
            DriftJobConfig(
                baseline_path=args.baseline,
                current_path=args.current,
                threshold=args.threshold,
                metrics_out_path=args.metrics_out,
            )
        )
    except DriftJobError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    verdict = "DRIFT DETECTED" if result.drifted else "no drift"
    print(f"drift score {result.score:.4f} (threshold {result.threshold:.4f}): {verdict}")
    return 1 if result.drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
