"""Train the fraud-detection classifier and persist model + metadata.

Standalone script: ``uv run python models/training/train.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dataset import (
    DatasetSplits,
    TrainingConfig,
    TrainingError,
    build_dataset,
    load_config,
    resolve_artifact_dir,
)
from sklearn_bridge import dump_model, fit_fraud_classifier

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
MODEL_FILENAME = "model.pkl"
META_FILENAME = "model_meta.json"


def build_model_meta(config: TrainingConfig, splits: DatasetSplits) -> dict[str, object]:
    return {
        "model_id": config.model_id,
        "model_version": "1.0.0",
        "algorithm": "GradientBoostingClassifier",
        "positive_label": "fraud",
        "seed": config.seed,
        "feature_count": len(splits.feature_names),
        "feature_names": list(splits.feature_names),
        "n_train_samples": int(splits.x_train.shape[0]),
        "n_val_samples": int(splits.x_val.shape[0]),
    }


def main() -> None:
    config = load_config(CONFIG_PATH)
    splits = build_dataset(config)
    model = fit_fraud_classifier(
        splits.x_train,
        splits.y_train,
        config.hyperparameters,
        random_state=config.seed,
    )

    artifact_dir = resolve_artifact_dir(config.artifact_dir)
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TrainingError(f"cannot create artifact dir {artifact_dir}: {exc}") from exc

    model_path = artifact_dir / MODEL_FILENAME
    meta_path = artifact_dir / META_FILENAME
    meta = build_model_meta(config, splits)
    try:
        dump_model(model, model_path)
        meta_path.write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise TrainingError(f"cannot write artifacts to {artifact_dir}: {exc}") from exc

    print(f"wrote {model_path}")
    print(f"wrote {meta_path}")


if __name__ == "__main__":
    try:
        main()
    except TrainingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
