"""batch_inference tests: dataset parsing, malformed-row resilience, batched scoring.

Given/When/Then per test: the committed fraud-detector artifact (loaded offline
via the storage abstraction, exactly like the job) scores synthetic rows; the
loader gets JSONL + CSV inputs with malformed lines interleaved; the CLI gets
tmp-path datasets. Batch-vs-rowwise parity is checked against the online
``predictor.predict`` so the offline path cannot drift from the served one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import numpy as np
import pytest
from inferenceops_model_server.model_loader import TrainedModel, load_model
from inferenceops_model_server.predictor import predict
from inferenceops_model_server.storage.local import LocalStorageBackend
from inferenceops_worker.batch_inference import (
    BatchInferenceConfig,
    BatchInferenceError,
    load_rows,
    main,
    meta_path_for,
    predict_batch,
    run_batch_inference,
)

_REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_MODELS_DIR: Final = _REPO_ROOT / "models" / "artifacts"
_MODEL_ID: Final = "fraud-detector"
_MODEL_VERSION: Final = "1.0.0"
_WIDTH: Final = 20


@pytest.fixture(scope="module")
def model() -> TrainedModel:
    """The committed fraud-detector artifact, loaded offline (cached per process)."""
    return load_model(LocalStorageBackend(_MODELS_DIR), _MODEL_ID, _MODEL_VERSION)


def _rows(count: int, seed: int = 42, width: int = _WIDTH) -> list[list[float]]:
    """A standard-normal feature matrix as plain Python rows."""
    rng = np.random.default_rng(seed)
    return [[float(value) for value in row] for row in rng.normal(size=(count, width))]


def _write_jsonl(path: Path, rows: list[list[float]]) -> Path:
    """Serialize rows as golden-format JSONL (``{"features": [...]}``)."""
    lines = [json.dumps({"features": row}) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Parse a JSONL file into a list of raw records."""
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _config(tmp_path: Path, input_path: Path, batch_size: int = 100) -> BatchInferenceConfig:
    """A batch config scoring ``input_path`` into ``tmp_path`` against the real artifact."""
    return BatchInferenceConfig(
        input_path=input_path,
        output_path=tmp_path / "predictions.jsonl",
        model_id=_MODEL_ID,
        model_version=_MODEL_VERSION,
        batch_size=batch_size,
        storage_root=_MODELS_DIR,
    )


def test_load_rows_parses_jsonl(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "data.jsonl", [[1.0, 2.0], [3.0, 4.0]])
    rows, skips = load_rows(path)
    assert rows == [(1, [1.0, 2.0]), (2, [3.0, 4.0])]
    assert skips == []


def test_load_rows_parses_csv(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("1.0,2.0\n3.0,4.0\n", encoding="utf-8")
    rows, skips = load_rows(path)
    assert rows == [(1, [1.0, 2.0]), (2, [3.0, 4.0])]
    assert skips == []


def test_load_rows_ignores_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text('{"features": [1.0]}\n\n   \n{"features": [2.0]}\n', encoding="utf-8")
    rows, skips = load_rows(path)
    assert rows == [(1, [1.0]), (4, [2.0])]
    assert skips == []


def test_load_rows_skips_malformed_json_and_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text('{"features": [1.0]}\nnot json\n{"features": [2.0]}\n', encoding="utf-8")
    rows, skips = load_rows(path)
    assert rows == [(1, [1.0]), (3, [2.0])]
    assert len(skips) == 1
    assert skips[0].line == 2
    assert "invalid row" in skips[0].reason
    assert "skip" in capsys.readouterr().err


def test_load_rows_skips_non_numeric_csv(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("1.0,oops\n3.0,4.0\n", encoding="utf-8")
    rows, skips = load_rows(path)
    assert rows == [(2, [3.0, 4.0])]
    assert len(skips) == 1
    assert "non-numeric" in skips[0].reason


def test_load_rows_skips_non_finite_and_wrong_shape_json(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text(
        '{"features": [NaN]}\n{"features": "scalar"}\n{"wrong": 1}\n{"features": [1.0]}\n',
        encoding="utf-8",
    )
    rows, skips = load_rows(path)
    assert rows == [(4, [1.0])]
    assert len(skips) == 3


def test_load_rows_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BatchInferenceError, match="cannot read dataset"):
        load_rows(tmp_path / "missing.jsonl")


def test_predict_batch_matches_rowwise_predictor(model: TrainedModel) -> None:
    rows = _rows(7, seed=11)
    batched = predict_batch(model, rows)
    rowwise = [predict(model, row) for row in rows]
    assert [item.label for item in batched] == [item.label for item in rowwise]
    for batched_item, rowwise_item in zip(batched, rowwise, strict=True):
        assert batched_item.score == pytest.approx(rowwise_item.score)


def test_run_batch_inference_writes_predictions_and_meta(
    model: TrainedModel, tmp_path: Path
) -> None:
    rows = _rows(25, seed=42)
    config = _config(tmp_path, _write_jsonl(tmp_path / "data.jsonl", rows), batch_size=10)
    result = run_batch_inference(config)
    assert result.meta.batches == 3
    assert result.meta.scored_rows == 25
    assert result.meta.skipped_rows == 0
    assert result.meta.model_id == _MODEL_ID
    records = _read_jsonl(result.output_path)
    assert len(records) == 25
    for index, record in enumerate(records):
        assert record["features"] == rows[index]
        expected = predict(model, rows[index])
        assert record["prediction"] == expected.label
        assert record["score"] == pytest.approx(expected.score)
        latency_ms = record["latency_ms"]
        assert isinstance(latency_ms, float)
        assert latency_ms > 0.0
    stored_meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert stored_meta["scored_rows"] == 25
    assert stored_meta["batches"] == 3


def test_run_batch_inference_reads_csv(model: TrainedModel, tmp_path: Path) -> None:
    rows = _rows(5, seed=7)
    csv_path = tmp_path / "data.csv"
    csv_path.write_text(
        "\n".join(",".join(repr(value) for value in row) for row in rows) + "\n",
        encoding="utf-8",
    )
    result = run_batch_inference(_config(tmp_path, csv_path))
    assert result.meta.scored_rows == 5
    expected = predict(model, rows[0])
    assert _read_jsonl(result.output_path)[0]["prediction"] == expected.label


def test_run_batch_inference_skips_bad_rows_and_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "data.jsonl"
    valid = _rows(3, seed=42)
    path.write_text(
        "\n".join(
            [
                json.dumps({"features": valid[0]}),
                json.dumps({"features": valid[1][: _WIDTH - 1]}),  # one feature short
                "not json",
                json.dumps({"features": valid[1]}),
                json.dumps({"features": valid[2]}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = run_batch_inference(_config(tmp_path, path))
    assert result.meta.scored_rows == 3
    assert result.meta.skipped_rows == 2
    reasons = {skip.line: skip.reason for skip in result.meta.skipped}
    assert "expected 20 features, got 19" in reasons[2]
    assert "invalid row" in reasons[3]
    skip_lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("skip")]
    assert len(skip_lines) == 2
    assert meta_path_for(result.output_path).is_file()


def test_run_batch_inference_rejects_dataset_with_no_valid_rows(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text(json.dumps({"features": [1.0] * (_WIDTH - 1)}) + "\n", encoding="utf-8")
    with pytest.raises(BatchInferenceError, match="no valid rows"):
        run_batch_inference(_config(tmp_path, path))


def test_run_batch_inference_rejects_missing_model(tmp_path: Path) -> None:
    # A model_id never loaded before: model_loader caches by (model_id, version),
    # so a fresh identity is what actually exercises the storage lookup.
    path = _write_jsonl(tmp_path / "data.jsonl", _rows(2))
    config = BatchInferenceConfig(
        input_path=path,
        output_path=tmp_path / "predictions.jsonl",
        model_id="missing-model",
        storage_root=tmp_path / "empty-artifacts",
    )
    with pytest.raises(BatchInferenceError, match="not found"):
        run_batch_inference(config)


def test_run_batch_inference_rejects_zero_batch_size(tmp_path: Path) -> None:
    path = _write_jsonl(tmp_path / "data.jsonl", _rows(2))
    with pytest.raises(BatchInferenceError, match="batch_size"):
        run_batch_inference(_config(tmp_path, path, batch_size=0))


def test_main_scores_and_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dataset = _write_jsonl(tmp_path / "data.jsonl", _rows(12))
    output = tmp_path / "predictions.jsonl"
    exit_code = main(
        [
            "--input", str(dataset),
            "--output", str(output),
            "--storage-root", str(_MODELS_DIR),
            "--batch-size", "5",
        ]
    )
    assert exit_code == 0
    assert "scored 12 rows in 3 batches" in capsys.readouterr().out
    assert len(_read_jsonl(output)) == 12


def test_main_returns_two_on_operational_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["--input", str(tmp_path / "missing.jsonl"), "--output", "out.jsonl"])
    assert exit_code == 2
    assert "error:" in capsys.readouterr().err
