"""drift_job tests: PSI/KS math, offline detection, gauge publication, CLI codes.

Given/When/Then per test: synthetic same-seed distributions for the in-
distribution case (score ~0), a multi-sigma shift for the drifted case (score
high), the real golden dataset against itself for the exact-zero baseline, and
tmp-path JSONL files for the loader + job + CLI contracts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import numpy as np
import pytest
from inferenceops_worker.drift_job import (
    DEFAULT_DRIFT_THRESHOLD,
    DriftJobConfig,
    DriftJobError,
    detect_drift,
    kolmogorov_smirnov_statistic,
    load_features,
    main,
    population_stability_index,
    run_drift_job,
)
from numpy.typing import NDArray

_REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_GOLDEN_DATASET: Final = _REPO_ROOT / "evals" / "golden_dataset.jsonl"
_FEATURE_COUNT: Final = 20


def _matrix(rows: int = 400, seed: int = 42) -> NDArray[np.float64]:
    """A standard-normal feature matrix (the in-distribution generator)."""
    rng = np.random.default_rng(seed)
    return rng.normal(size=(rows, _FEATURE_COUNT))


def _write_jsonl(path: Path, matrix: NDArray[np.float64]) -> Path:
    """Serialize a feature matrix as golden-format JSONL rows."""
    lines = [
        json.dumps({"features": [float(value) for value in row]}) for row in matrix
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _drift_score_from(text: str) -> float:
    """Parse the ``inferenceops_drift_score`` gauge value from rendered metrics."""
    for line in text.splitlines():
        if line.startswith("inferenceops_drift_score "):
            return float(line.rsplit(" ", maxsplit=1)[1])
    raise AssertionError("no inferenceops_drift_score sample line")


def test_psi_is_zero_for_identical_columns() -> None:
    rng = np.random.default_rng(42)
    column = rng.normal(size=500)
    assert population_stability_index(column, column.copy()) == 0.0


def test_psi_is_high_for_shifted_column() -> None:
    rng = np.random.default_rng(42)
    baseline = rng.normal(size=500)
    current = baseline + 3.0 * float(np.std(baseline))
    assert population_stability_index(baseline, current) > 1.0


def test_ks_is_zero_for_identical_columns() -> None:
    column = np.array([1.0, 2.0, 3.0, 4.0])
    assert kolmogorov_smirnov_statistic(column, column.copy()) == 0.0


def test_ks_is_high_for_shifted_column() -> None:
    rng = np.random.default_rng(7)
    baseline = rng.normal(size=300)
    current = baseline + 5.0
    assert kolmogorov_smirnov_statistic(baseline, current) > 0.9


def test_detect_drift_scores_low_in_distribution() -> None:
    result = detect_drift(_matrix(seed=42), _matrix(seed=7))
    assert result.score < DEFAULT_DRIFT_THRESHOLD
    assert result.drifted is False
    assert len(result.features) == _FEATURE_COUNT
    assert all(feature.ks < 0.2 for feature in result.features)


def test_detect_drift_flags_shifted_distribution() -> None:
    baseline = _matrix(seed=42)
    result = detect_drift(baseline, baseline + 3.0)
    assert result.score > DEFAULT_DRIFT_THRESHOLD
    assert result.drifted is True
    assert all(feature.psi > 0.25 for feature in result.features)


def test_detect_drift_on_golden_dataset_against_itself_is_exactly_zero() -> None:
    golden = load_features(_GOLDEN_DATASET)
    result = detect_drift(golden, golden)
    assert result.score == 0.0
    assert result.drifted is False


def test_detect_drift_rejects_feature_count_mismatch() -> None:
    with pytest.raises(DriftJobError, match="feature count mismatch"):
        detect_drift(np.zeros((10, _FEATURE_COUNT)), np.zeros((10, 3)))


def test_load_features_parses_rows_and_ignores_extra_fields(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text(
        json.dumps({"features": [1.0, 2.0], "expected": 0})
        + "\n"
        + json.dumps({"features": [3.0, 4.0], "expected": 1})
        + "\n",
        encoding="utf-8",
    )
    matrix = load_features(path)
    assert matrix.shape == (2, 2)
    assert float(matrix[1, 1]) == 4.0


def test_load_features_rejects_ragged_rows(tmp_path: Path) -> None:
    path = tmp_path / "ragged.jsonl"
    path.write_text('{"features": [1.0, 2.0]}\n{"features": [1.0]}\n', encoding="utf-8")
    with pytest.raises(DriftJobError, match="expected 2 features"):
        load_features(path)


def test_load_features_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(DriftJobError, match="invalid row"):
        load_features(path)


def test_load_features_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(DriftJobError, match="contains no rows"):
        load_features(path)


def test_load_features_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DriftJobError, match="cannot read dataset"):
        load_features(tmp_path / "missing.jsonl")


def test_run_drift_job_publishes_drift_score_metric(tmp_path: Path) -> None:
    baseline_path = _write_jsonl(tmp_path / "baseline.jsonl", _matrix(seed=42))
    current_path = _write_jsonl(tmp_path / "current.jsonl", _matrix(seed=42) + 3.0)
    metrics_out = tmp_path / "metrics" / "drift.txt"
    result = run_drift_job(
        DriftJobConfig(
            baseline_path=baseline_path, current_path=current_path, metrics_out_path=metrics_out
        )
    )
    assert result.drifted is True
    text = metrics_out.read_text(encoding="utf-8")
    assert "# TYPE inferenceops_drift_score gauge" in text
    assert _drift_score_from(text) == pytest.approx(result.score)


def test_main_returns_zero_when_stable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    matrix = _matrix(seed=42)
    baseline_path = _write_jsonl(tmp_path / "baseline.jsonl", matrix)
    current_path = _write_jsonl(tmp_path / "current.jsonl", matrix.copy())
    exit_code = main(["--baseline", str(baseline_path), "--current", str(current_path)])
    assert exit_code == 0
    assert "no drift" in capsys.readouterr().out


def test_main_returns_one_and_alerts_on_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline_path = _write_jsonl(tmp_path / "baseline.jsonl", _matrix(seed=42))
    current_path = _write_jsonl(tmp_path / "current.jsonl", _matrix(seed=42) + 3.0)
    exit_code = main(["--baseline", str(baseline_path), "--current", str(current_path)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "DRIFT ALERT" in captured.err
    assert "DRIFT DETECTED" in captured.out


def test_main_returns_two_on_operational_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = str(tmp_path / "missing.jsonl")
    exit_code = main(["--baseline", missing, "--current", missing])
    assert exit_code == 2
    assert "error:" in capsys.readouterr().err
