"""Evaluate the trained fraud-detection classifier on the held-out val split.

Standalone script: ``uv run python models/training/evaluate.py``.
Writes ``metrics.json`` (accuracy, f1, roc_auc, seed, n_samples) next to the
model artifact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dataset import (
    TrainingError,
    build_dataset,
    load_config,
    resolve_artifact_dir,
)
from sklearn_bridge import (
    accuracy,
    f1,
    load_model,
    positive_class_scores,
    predict_labels,
    roc_auc,
)

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
MODEL_FILENAME = "model.pkl"
METRICS_FILENAME = "metrics.json"


def main() -> None:
    config = load_config(CONFIG_PATH)
    splits = build_dataset(config)

    artifact_dir = resolve_artifact_dir(config.artifact_dir)
    model_path = artifact_dir / MODEL_FILENAME
    try:
        model = load_model(model_path)
    except OSError as exc:
        raise TrainingError(
            f"cannot load model artifact {model_path}; run train.py first: {exc}"
        ) from exc

    y_pred = predict_labels(model, splits.x_val)
    scores = positive_class_scores(model, splits.x_val)
    metrics: dict[str, float | int] = {
        "accuracy": accuracy(splits.y_val, y_pred),
        "f1": f1(splits.y_val, y_pred),
        "roc_auc": roc_auc(splits.y_val, scores),
        "seed": config.seed,
        "n_samples": int(splits.x_val.shape[0]),
    }

    metrics_path = artifact_dir / METRICS_FILENAME
    try:
        metrics_path.write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise TrainingError(f"cannot write metrics to {metrics_path}: {exc}") from exc

    print(f"wrote {metrics_path}")
    for key in sorted(metrics):
        print(f"{key}={metrics[key]}")


if __name__ == "__main__":
    try:
        main()
    except TrainingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
