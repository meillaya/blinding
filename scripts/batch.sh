#!/usr/bin/env bash
# Offline batch inference + report (todo 41) — the `make batch` target.
#
# Scores a dataset WITHOUT the online server: the model is loaded directly via
# the storage abstraction (models/artifacts) and scored in batches, then the
# predictions are summarized into a markdown + JSON report.
#
# Steps:
#   1. generate a deterministic ~1000-row synthetic dataset (reusing the
#      training generator, models/training/dataset.py) unless one is provided;
#   2. run inferenceops_worker.batch_inference (malformed rows are skipped +
#      logged, never fatal);
#   3. run inferenceops_worker.report_generator over the predictions.
#
# Knobs (env overrides): DATASET, PREDICTIONS, REPORT_MD, REPORT_JSON, ROWS,
# BATCH_SIZE, MODEL_ID, MODEL_VERSION. Outputs default under evals/reports/
# (untracked generated artifacts — never commit them).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DATASET="${DATASET:-evals/reports/batch_dataset.jsonl}"
PREDICTIONS="${PREDICTIONS:-evals/reports/batch_predictions.jsonl}"
REPORT_MD="${REPORT_MD:-evals/reports/batch_report.md}"
REPORT_JSON="${REPORT_JSON:-evals/reports/batch_report.json}"
ROWS="${ROWS:-1000}"
BATCH_SIZE="${BATCH_SIZE:-100}"
MODEL_ID="${MODEL_ID:-fraud-detector}"
MODEL_VERSION="${MODEL_VERSION:-1.0.0}"

# Both workspace packages are package=false (run from source, never installed).
export PYTHONPATH=apps/worker/src:apps/model-server/src

if [[ ! -f "$DATASET" ]]; then
	echo "generating $ROWS-row dataset -> $DATASET"
	uv run python - "$DATASET" "$ROWS" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, "models/training")
from dataset import build_dataset, load_config  # noqa: E402

out = Path(sys.argv[1])
rows = int(sys.argv[2])
splits = build_dataset(load_config(Path("models/training/config.yaml")))
matrix = splits.x_val[:rows]
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w", encoding="utf-8") as handle:
    for row in matrix:
        handle.write(json.dumps({"features": [float(value) for value in row]}) + "\n")
print(f"wrote {matrix.shape[0]} rows -> {out}")
PY
else
	echo "using existing dataset: $DATASET"
fi

uv run python -m inferenceops_worker.batch_inference \
	--input "$DATASET" \
	--output "$PREDICTIONS" \
	--model-id "$MODEL_ID" \
	--model-version "$MODEL_VERSION" \
	--batch-size "$BATCH_SIZE"

uv run python -m inferenceops_worker.report_generator \
	--predictions "$PREDICTIONS" \
	--md-out "$REPORT_MD" \
	--json-out "$REPORT_JSON"

echo "batch complete: $PREDICTIONS, $REPORT_MD, $REPORT_JSON"
