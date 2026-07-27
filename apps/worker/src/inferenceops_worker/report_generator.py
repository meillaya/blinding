"""Offline batch-inference report generation (spec §3, todo 41).

Summarizes a predictions JSONL written by ``batch_inference`` (one record per
scored row: ``line``, ``features``, ``prediction``, ``score``, ``latency_ms``)
into a human-readable markdown report and a machine-readable JSON report:

- total prediction count and the skipped-input-row count (read from the run's
  ``*.meta.json`` sidecar when present — the predictions file only holds scored
  rows, so the metadata is the source of truth for what was dropped);
- prediction distribution (count + share per class label);
- score distribution (positive-class probability: min / median / mean / p95 /
  max);
- latency statistics (per-row amortized batch latency, ms: min / median / mean
  / max).

Runs fully offline — no online server required::

    PYTHONPATH=apps/worker/src:apps/model-server/src uv run python \\
        -m inferenceops_worker.report_generator \\
        --predictions predictions.jsonl

``--md-out`` / ``--json-out`` default to ``<predictions stem>.report.md`` /
``.report.json``. Exit codes: 0 = report written, 2 = operational error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inferenceops_worker.batch_inference import BatchMeta, meta_path_for


class ReportError(Exception):
    """Raised when a report cannot be generated from its inputs."""


class _PredictionRecord(BaseModel):
    """Boundary parse for one predictions JSONL record (the batch_inference format)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: int
    features: list[float] = Field(min_length=1)
    prediction: int
    score: float = Field(ge=0.0, le=1.0)
    latency_ms: float = Field(ge=0.0)


@dataclass(frozen=True, slots=True)
class ScoreStats:
    """Positive-class probability distribution across every scored row."""

    minimum: float
    median: float
    mean: float
    p95: float
    maximum: float


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Per-row amortized batch latency (ms) across every scored row."""

    minimum_ms: float
    median_ms: float
    mean_ms: float
    maximum_ms: float


@dataclass(frozen=True, slots=True)
class BatchReport:
    """The full summary of one batch-inference run."""

    total: int
    label_counts: tuple[tuple[int, int], ...]
    score: ScoreStats
    latency: LatencyStats
    skipped_rows: int
    model_id: str | None
    model_version: str | None
    batches: int | None
    elapsed_seconds: float | None


@dataclass(frozen=True, slots=True)
class ReportConfig:
    """Inputs for one report generation."""

    predictions_path: Path
    markdown_path: Path
    json_path: Path
    meta_path: Path | None = None


def load_predictions(path: Path) -> list[_PredictionRecord]:
    """Parse a predictions JSONL; any malformed record is a hard error (our format)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"cannot read predictions {path}: {exc}") from exc
    records: list[_PredictionRecord] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(_PredictionRecord.model_validate(json.loads(line)))
        except (ValidationError, json.JSONDecodeError) as exc:
            raise ReportError(f"{path}:{line_number}: invalid prediction record: {exc}") from exc
    if not records:
        raise ReportError(f"predictions file {path} contains no records")
    return records


def load_meta(path: Path) -> BatchMeta | None:
    """Read the batch metadata sidecar; None when absent (skipped count unknown)."""
    if not path.is_file():
        return None
    try:
        return BatchMeta.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ReportError(f"cannot read batch metadata {path}: {exc}") from exc


def summarize(records: Sequence[_PredictionRecord], meta: BatchMeta | None) -> BatchReport:
    """Reduce scored records (+ optional run metadata) to the report summary."""
    scores = np.asarray([record.score for record in records], dtype=np.float64)
    latencies = np.asarray([record.latency_ms for record in records], dtype=np.float64)
    counts = Counter(record.prediction for record in records)
    return BatchReport(
        total=len(records),
        label_counts=tuple(sorted(counts.items())),
        score=ScoreStats(
            minimum=float(scores.min()),
            median=float(np.median(scores)),
            mean=float(scores.mean()),
            p95=float(np.percentile(scores, 95.0)),
            maximum=float(scores.max()),
        ),
        latency=LatencyStats(
            minimum_ms=float(latencies.min()),
            median_ms=float(np.median(latencies)),
            mean_ms=float(latencies.mean()),
            maximum_ms=float(latencies.max()),
        ),
        skipped_rows=meta.skipped_rows if meta is not None else 0,
        model_id=meta.model_id if meta is not None else None,
        model_version=meta.model_version if meta is not None else None,
        batches=meta.batches if meta is not None else None,
        elapsed_seconds=meta.elapsed_seconds if meta is not None else None,
    )


def _model_label(report: BatchReport) -> str:
    """The model identity line for the report (``id @ version`` or ``unknown``)."""
    if report.model_id is None or report.model_version is None:
        return "unknown (no batch metadata sidecar)"
    return f"{report.model_id} @ {report.model_version}"


def render_markdown(report: BatchReport, predictions_path: Path) -> str:
    """Render the report summary as markdown."""
    lines = [
        "# Batch Inference Report",
        "",
        f"Source: `{predictions_path}` — Model: {_model_label(report)}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total predictions | {report.total} |",
        f"| Skipped input rows | {report.skipped_rows} |",
    ]
    if report.batches is not None:
        lines.append(f"| Inference batches | {report.batches} |")
    if report.elapsed_seconds is not None:
        lines.append(f"| Total scoring time (s) | {report.elapsed_seconds:.3f} |")
    lines += ["", "## Prediction distribution", "", "| Label | Count | Share |", "|---:|---:|---:|"]
    lines += [
        f"| {label} | {count} | {count / report.total:.2%} |"
        for label, count in report.label_counts
    ]
    score = report.score
    lines += [
        "",
        "## Score distribution (positive-class probability)",
        "",
        "| min | median | mean | p95 | max |",
        "|---:|---:|---:|---:|---:|",
        f"| {score.minimum:.4f} | {score.median:.4f} | {score.mean:.4f} "
        f"| {score.p95:.4f} | {score.maximum:.4f} |",
    ]
    latency = report.latency
    lines += [
        "",
        "## Latency (ms per row, batch time amortized across the batch)",
        "",
        "| min | median | mean | max |",
        "|---:|---:|---:|---:|",
        f"| {latency.minimum_ms:.3f} | {latency.median_ms:.3f} "
        f"| {latency.mean_ms:.3f} | {latency.maximum_ms:.3f} |",
        "",
    ]
    return "\n".join(lines)


def report_to_json(report: BatchReport, predictions_path: Path) -> dict[str, object]:
    """Render the report summary as a JSON-serializable dict."""
    return {
        "predictions_path": str(predictions_path),
        "model_id": report.model_id,
        "model_version": report.model_version,
        "total": report.total,
        "skipped_rows": report.skipped_rows,
        "batches": report.batches,
        "elapsed_seconds": report.elapsed_seconds,
        "label_counts": {str(label): count for label, count in report.label_counts},
        "score": {
            "min": report.score.minimum,
            "median": report.score.median,
            "mean": report.score.mean,
            "p95": report.score.p95,
            "max": report.score.maximum,
        },
        "latency_ms": {
            "min": report.latency.minimum_ms,
            "median": report.latency.median_ms,
            "mean": report.latency.mean_ms,
            "max": report.latency.maximum_ms,
        },
    }


def generate_report(config: ReportConfig) -> BatchReport:
    """Summarize a predictions file and write the markdown + JSON reports."""
    records = load_predictions(config.predictions_path)
    meta_path = (
        config.meta_path if config.meta_path is not None else meta_path_for(config.predictions_path)
    )
    report = summarize(records, load_meta(meta_path))
    try:
        config.markdown_path.parent.mkdir(parents=True, exist_ok=True)
        config.markdown_path.write_text(
            render_markdown(report, config.predictions_path), encoding="utf-8"
        )
        config.json_path.parent.mkdir(parents=True, exist_ok=True)
        config.json_path.write_text(
            json.dumps(report_to_json(report, config.predictions_path), indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ReportError(f"cannot write reports: {exc}") from exc
    return report


@dataclass(frozen=True, slots=True)
class _ReportArgs:
    """Typed report CLI arguments (parsed once at the boundary)."""

    predictions: Path
    md_out: Path
    json_out: Path
    meta: Path | None


def _parse_args(argv: Sequence[str] | None) -> _ReportArgs:
    parser = argparse.ArgumentParser(description="Summarize a batch predictions file.")
    parser.add_argument("--predictions", required=True, type=Path, help="Predictions JSONL.")
    parser.add_argument("--md-out", type=Path, default=None, help="Markdown report path.")
    parser.add_argument("--json-out", type=Path, default=None, help="JSON report path.")
    parser.add_argument("--meta", type=Path, default=None, help="Batch metadata sidecar path.")
    ns = parser.parse_args(argv)
    predictions: Path = ns.predictions
    md_out: Path = ns.md_out if ns.md_out is not None else predictions.with_suffix(".report.md")
    json_out: Path = (
        ns.json_out if ns.json_out is not None else predictions.with_suffix(".report.json")
    )
    return _ReportArgs(predictions=predictions, md_out=md_out, json_out=json_out, meta=ns.meta)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 = report written, 2 = operational error."""
    args = _parse_args(argv)
    try:
        report = generate_report(
            ReportConfig(
                predictions_path=args.predictions,
                markdown_path=args.md_out,
                json_path=args.json_out,
                meta_path=args.meta,
            )
        )
    except ReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"report: {report.total} predictions ({report.skipped_rows} skipped rows) "
        f"-> {args.md_out}, {args.json_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
