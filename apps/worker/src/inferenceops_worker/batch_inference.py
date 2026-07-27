"""Offline batch inference job (spec §3, todo 41).

Scores a whole dataset (JSONL ``{"features": [...]}`` rows or headerless numeric
CSV) WITHOUT the online server: the model is loaded directly through the storage
abstraction (``LocalStorageBackend`` + ``model_loader``, todo 7) and scored in
fixed-size batches — one vectorized model call per batch, the offline mirror of
the online ``DynamicBatcher``'s single-call-per-batch fan-out.

Malformed input rows (bad JSON, non-numeric or non-finite fields, wrong feature
count) are logged to stderr, recorded in the run metadata, and SKIPPED — the job
continues with the well-formed rows and only fails when NO row is scoreable.

Outputs, next to ``--output``:

- ``<output>`` — predictions JSONL, one record per scored row: ``line`` (source
  line number), ``features``, ``prediction`` (class label), ``score``
  (positive-class probability), ``latency_ms`` (the batch's wall-clock time
  amortized across its rows);
- ``<output stem>.meta.json`` — run metadata (model identity, batch counts,
  skipped rows with reasons) that ``report_generator`` reads to summarize the
  run::

    PYTHONPATH=apps/worker/src:apps/model-server/src uv run python \\
        -m inferenceops_worker.batch_inference \\
        --input dataset.jsonl --output predictions.jsonl

Exit codes: 0 = scored (possibly with skipped rows), 2 = operational error.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
from inferenceops_model_server.errors import ModelNotFound
from inferenceops_model_server.model_loader import TrainedModel, load_model
from inferenceops_model_server.predictor import Prediction
from inferenceops_model_server.schemas import FiniteFloat
from inferenceops_model_server.storage.local import LocalStorageBackend
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_BATCH_SIZE: Final = 100
DEFAULT_MODEL_ID: Final = "fraud-detector"
DEFAULT_MODEL_VERSION: Final = "1.0.0"


class BatchInferenceError(Exception):
    """Raised when a batch job cannot load its model or dataset, or write output."""


class _FeatureRow(BaseModel):
    """Boundary parse for one dataset row; extra keys (e.g. ``expected``) ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    features: list[FiniteFloat] = Field(min_length=1)


class SkippedRow(BaseModel):
    """One malformed input row the job logged and skipped (carried in the metadata)."""

    model_config = ConfigDict(frozen=True)

    line: int
    reason: str


class BatchMeta(BaseModel):
    """Run metadata written beside the predictions; the report generator reads it."""

    model_config = ConfigDict(frozen=True)

    input_path: str
    model_id: str
    model_version: str
    batch_size: int
    batches: int
    scored_rows: int
    skipped_rows: int
    skipped: tuple[SkippedRow, ...]
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class BatchPrediction:
    """One scored row: source line, its features, and the model's verdict."""

    line: int
    features: tuple[float, ...]
    prediction: int
    score: float
    latency_ms: float


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Outcome of a batch-inference run."""

    predictions: tuple[BatchPrediction, ...]
    meta: BatchMeta
    output_path: Path
    meta_path: Path


@dataclass(frozen=True, slots=True)
class BatchInferenceConfig:
    """Inputs for one offline batch-inference run."""

    input_path: Path
    output_path: Path
    model_id: str = DEFAULT_MODEL_ID
    model_version: str = DEFAULT_MODEL_VERSION
    batch_size: int = DEFAULT_BATCH_SIZE
    storage_root: Path | None = None


def meta_path_for(output_path: Path) -> Path:
    """The metadata sidecar path for a predictions file (``*.meta.json``)."""
    return output_path.with_suffix(".meta.json")


def _parse_jsonl(raw: str, skips: list[SkippedRow]) -> Iterator[tuple[int, list[float]]]:
    """Yield ``(line_number, features)`` per valid JSONL row; record bad rows."""
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = _FeatureRow.model_validate(json.loads(line))
        except (ValidationError, json.JSONDecodeError) as exc:
            skips.append(SkippedRow(line=line_number, reason=f"invalid row: {exc}"))
            continue
        yield line_number, list(parsed.features)


def _parse_csv(raw: str, skips: list[SkippedRow]) -> Iterator[tuple[int, list[float]]]:
    """Yield ``(line_number, features)`` per valid numeric CSV row; record bad rows."""
    for line_number, fields in enumerate(csv.reader(raw.splitlines()), start=1):
        if not fields or not any(field.strip() for field in fields):
            continue
        try:
            values = [float(field) for field in fields]
        except ValueError:
            skips.append(SkippedRow(line=line_number, reason="non-numeric field"))
            continue
        try:
            parsed = _FeatureRow.model_validate({"features": values})
        except ValidationError as exc:
            skips.append(SkippedRow(line=line_number, reason=f"invalid row: {exc}"))
            continue
        yield line_number, list(parsed.features)


def load_rows(path: Path) -> tuple[list[tuple[int, list[float]]], list[SkippedRow]]:
    """Parse a JSONL (default) or ``.csv`` dataset; skip + log malformed rows."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BatchInferenceError(f"cannot read dataset {path}: {exc}") from exc
    skips: list[SkippedRow] = []
    parser = _parse_csv if path.suffix.lower() == ".csv" else _parse_jsonl
    rows = list(parser(raw, skips))
    for skip in skips:
        print(f"skip {path}:{skip.line}: {skip.reason}", file=sys.stderr)
    return rows, skips


def predict_batch(model: TrainedModel, rows: list[list[float]]) -> list[Prediction]:
    """Score one batch of width-validated rows in a single vectorized model call."""
    matrix = np.asarray(rows, dtype=np.float64)
    labels = model.predict(matrix)
    scores = model.predict_proba(matrix)[:, 1]
    return [
        Prediction(label=int(labels[index]), score=float(scores[index]))
        for index in range(len(rows))
    ]


def _write_outputs(
    output_path: Path, predictions: Sequence[BatchPrediction], meta: BatchMeta
) -> None:
    """Write the predictions JSONL and its metadata sidecar (predictions first)."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            for item in predictions:
                record = {
                    "line": item.line,
                    "features": list(item.features),
                    "prediction": item.prediction,
                    "score": item.score,
                    "latency_ms": item.latency_ms,
                }
                handle.write(json.dumps(record) + "\n")
        meta_path = meta_path_for(output_path)
        meta_path.write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise BatchInferenceError(f"cannot write outputs for {output_path}: {exc}") from exc


def run_batch_inference(config: BatchInferenceConfig) -> BatchResult:
    """Load the model offline, score every well-formed row in batches, write outputs."""
    if config.batch_size < 1:
        raise BatchInferenceError(f"batch_size must be >= 1, got {config.batch_size}")
    try:
        model = load_model(
            LocalStorageBackend(config.storage_root), config.model_id, config.model_version
        )
    except ModelNotFound as exc:
        raise BatchInferenceError(str(exc)) from exc
    parsed, skipped = load_rows(config.input_path)
    valid: list[tuple[int, list[float]]] = []
    for line_number, features in parsed:
        if len(features) == model.n_features_in_:
            valid.append((line_number, features))
            continue
        skip = SkippedRow(
            line=line_number,
            reason=f"expected {model.n_features_in_} features, got {len(features)}",
        )
        skipped.append(skip)
        print(f"skip {config.input_path}:{skip.line}: {skip.reason}", file=sys.stderr)
    if not valid:
        raise BatchInferenceError(f"dataset {config.input_path} contains no valid rows")
    predictions: list[BatchPrediction] = []
    batches = 0
    started = time.perf_counter()
    for start in range(0, len(valid), config.batch_size):
        chunk = valid[start : start + config.batch_size]
        batch_started = time.perf_counter()
        results = predict_batch(model, [features for _, features in chunk])
        latency_ms = (time.perf_counter() - batch_started) * 1000.0 / len(chunk)
        batches += 1
        for (line_number, features), result in zip(chunk, results, strict=True):
            predictions.append(
                BatchPrediction(
                    line=line_number,
                    features=tuple(features),
                    prediction=result.label,
                    score=result.score,
                    latency_ms=latency_ms,
                )
            )
    meta = BatchMeta(
        input_path=str(config.input_path),
        model_id=config.model_id,
        model_version=config.model_version,
        batch_size=config.batch_size,
        batches=batches,
        scored_rows=len(predictions),
        skipped_rows=len(skipped),
        skipped=tuple(skipped),
        elapsed_seconds=time.perf_counter() - started,
    )
    _write_outputs(config.output_path, predictions, meta)
    return BatchResult(
        predictions=tuple(predictions),
        meta=meta,
        output_path=config.output_path,
        meta_path=meta_path_for(config.output_path),
    )


@dataclass(frozen=True, slots=True)
class _BatchArgs:
    """Typed batch-job CLI arguments (parsed once at the boundary)."""

    input: Path
    output: Path
    model_id: str
    model_version: str
    batch_size: int
    storage_root: Path | None


def _parse_args(argv: Sequence[str] | None) -> _BatchArgs:
    parser = argparse.ArgumentParser(description="Offline batch inference (no online server).")
    parser.add_argument("--input", required=True, type=Path, help="Dataset JSONL or CSV to score.")
    parser.add_argument("--output", required=True, type=Path, help="Predictions JSONL to write.")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Model identity to load.")
    parser.add_argument("--model-version", default=DEFAULT_MODEL_VERSION, help="Model version.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Rows per call.")
    parser.add_argument("--storage-root", type=Path, default=None, help="Artifact root override.")
    ns = parser.parse_args(argv)
    return _BatchArgs(
        input=ns.input,
        output=ns.output,
        model_id=str(ns.model_id),
        model_version=str(ns.model_version),
        batch_size=int(ns.batch_size),
        storage_root=ns.storage_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 = scored (possibly with skipped rows), 2 = operational error."""
    args = _parse_args(argv)
    try:
        result = run_batch_inference(
            BatchInferenceConfig(
                input_path=args.input,
                output_path=args.output,
                model_id=args.model_id,
                model_version=args.model_version,
                batch_size=args.batch_size,
                storage_root=args.storage_root,
            )
        )
    except BatchInferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"scored {result.meta.scored_rows} rows in {result.meta.batches} batches "
        f"({result.meta.skipped_rows} skipped) -> {result.output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
