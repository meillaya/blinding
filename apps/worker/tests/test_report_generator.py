"""report_generator tests: summary stats, md/json rendering, metadata, CLI codes.

Given/When/Then per test: hand-computed score/latency distributions for the
exact-stats case (min/median/mean/p95/max derived by hand, not recomputed from
the implementation), written predictions files for the rendering + metadata
contracts, and malformed inputs for the typed-error paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from inferenceops_worker.batch_inference import BatchMeta, SkippedRow, meta_path_for
from inferenceops_worker.report_generator import (
    ReportConfig,
    ReportError,
    generate_report,
    load_predictions,
    main,
    summarize,
)


def _record(line: int, prediction: int, score: float, latency_ms: float = 1.0) -> str:
    """One predictions JSONL record in the batch_inference output format."""
    return json.dumps(
        {
            "line": line,
            "features": [0.5, 0.25],
            "prediction": prediction,
            "score": score,
            "latency_ms": latency_ms,
        }
    )


def _write_predictions(path: Path, lines: list[str]) -> Path:
    """Write raw predictions JSONL lines."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _four_rows() -> list[str]:
    """The standard 4-record fixture: labels 0/0/1/1, scores .1/.2/.8/.9, ms 1-4."""
    return [
        _record(1, 0, 0.1, 1.0),
        _record(2, 0, 0.2, 2.0),
        _record(3, 1, 0.8, 3.0),
        _record(4, 1, 0.9, 4.0),
    ]


def _meta(skipped_rows: int = 3) -> BatchMeta:
    """A representative batch-metadata sidecar payload."""
    return BatchMeta(
        input_path="data.jsonl",
        model_id="fraud-detector",
        model_version="1.0.0",
        batch_size=100,
        batches=2,
        scored_rows=4,
        skipped_rows=skipped_rows,
        skipped=(SkippedRow(line=2, reason="invalid row"),),
        elapsed_seconds=0.5,
    )


def test_summarize_computes_counts_score_and_latency(tmp_path: Path) -> None:
    records = load_predictions(_write_predictions(tmp_path / "p.jsonl", _four_rows()))
    report = summarize(records, None)
    assert report.total == 4
    assert report.label_counts == ((0, 2), (1, 2))
    # Hand-derived: median (0.2+0.8)/2 = 0.5, mean 0.5,
    # p95 = 0.8 + 0.85 * (0.9 - 0.8) = 0.885 (numpy linear interpolation).
    assert report.score.minimum == pytest.approx(0.1)
    assert report.score.median == pytest.approx(0.5)
    assert report.score.mean == pytest.approx(0.5)
    assert report.score.p95 == pytest.approx(0.885)
    assert report.score.maximum == pytest.approx(0.9)
    # Latencies [1, 2, 3, 4]: median (2+3)/2 = 2.5, mean 2.5.
    assert report.latency.minimum_ms == pytest.approx(1.0)
    assert report.latency.median_ms == pytest.approx(2.5)
    assert report.latency.mean_ms == pytest.approx(2.5)
    assert report.latency.maximum_ms == pytest.approx(4.0)
    assert report.skipped_rows == 0
    assert report.model_id is None
    assert report.batches is None


def test_summarize_reads_metadata(tmp_path: Path) -> None:
    records = load_predictions(_write_predictions(tmp_path / "p.jsonl", _four_rows()))
    report = summarize(records, _meta(skipped_rows=7))
    assert report.skipped_rows == 7
    assert report.model_id == "fraud-detector"
    assert report.model_version == "1.0.0"
    assert report.batches == 2
    assert report.elapsed_seconds == pytest.approx(0.5)


def test_generate_report_writes_md_and_json(tmp_path: Path) -> None:
    predictions = _write_predictions(tmp_path / "p.jsonl", _four_rows())
    md_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"
    report = generate_report(
        ReportConfig(predictions_path=predictions, markdown_path=md_path, json_path=json_path)
    )
    assert report.total == 4
    markdown = md_path.read_text(encoding="utf-8")
    assert "# Batch Inference Report" in markdown
    assert "| Total predictions | 4 |" in markdown
    assert "| 0 | 2 | 50.00% |" in markdown
    assert "| 1 | 2 | 50.00% |" in markdown
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["total"] == 4
    assert payload["label_counts"] == {"0": 2, "1": 2}
    assert payload["score"]["p95"] == pytest.approx(0.885)
    assert payload["latency_ms"]["max"] == pytest.approx(4.0)
    assert payload["skipped_rows"] == 0


def test_generate_report_auto_discovers_meta_sidecar(tmp_path: Path) -> None:
    predictions = _write_predictions(tmp_path / "p.jsonl", _four_rows())
    meta_path_for(predictions).write_text(
        _meta(skipped_rows=5).model_dump_json(), encoding="utf-8"
    )
    report = generate_report(
        ReportConfig(
            predictions_path=predictions,
            markdown_path=tmp_path / "report.md",
            json_path=tmp_path / "report.json",
        )
    )
    assert report.skipped_rows == 5
    assert report.model_id == "fraud-detector"
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "| Skipped input rows | 5 |" in markdown
    assert "fraud-detector @ 1.0.0" in markdown


def test_load_predictions_rejects_malformed_record(tmp_path: Path) -> None:
    path = _write_predictions(tmp_path / "p.jsonl", [_record(1, 0, 0.5), "not json"])
    with pytest.raises(ReportError, match="invalid prediction record"):
        load_predictions(path)


def test_load_predictions_rejects_out_of_range_score(tmp_path: Path) -> None:
    path = _write_predictions(tmp_path / "p.jsonl", [_record(1, 0, 1.5)])
    with pytest.raises(ReportError, match="invalid prediction record"):
        load_predictions(path)


def test_load_predictions_rejects_empty_file(tmp_path: Path) -> None:
    path = _write_predictions(tmp_path / "p.jsonl", [])
    with pytest.raises(ReportError, match="contains no records"):
        load_predictions(path)


def test_load_predictions_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="cannot read predictions"):
        load_predictions(tmp_path / "missing.jsonl")


def test_main_returns_zero_and_writes_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    predictions = _write_predictions(tmp_path / "p.jsonl", _four_rows())
    md_path = tmp_path / "report.md"
    json_path = tmp_path / "report.json"
    exit_code = main(
        ["--predictions", str(predictions), "--md-out", str(md_path), "--json-out", str(json_path)]
    )
    assert exit_code == 0
    assert "4 predictions" in capsys.readouterr().out
    assert md_path.is_file()
    assert json_path.is_file()


def test_main_returns_two_on_operational_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["--predictions", str(tmp_path / "missing.jsonl")])
    assert exit_code == 2
    assert "error:" in capsys.readouterr().err
