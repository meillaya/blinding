"""Accuracy gate: score the trained fraud detector on the golden dataset.

Standalone: ``uv run python evals/run_eval.py``.

Loads ``models/artifacts/fraud_detector/model.pkl``, predicts every golden
row, computes accuracy / f1 / roc_auc, writes a timestamped report to
``evals/reports/eval_<UTC ts>.json``, and exits non-zero if accuracy falls
below ``ACCURACY_THRESHOLD``. ``regression_gate.py`` consumes the newest
report. See ``eval_common.py`` for how the golden labels were produced.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from eval_common import (
    REPORTS_DIR,
    EvalError,
    load_golden_dataset,
    load_trained_model,
    write_report,
)
from sklearn_seam import accuracy, f1, positive_class_scores, predict_labels, roc_auc

ACCURACY_THRESHOLD = 0.85


def main() -> None:
    model = load_trained_model()
    golden = load_golden_dataset()

    y_pred = predict_labels(model, golden.features)
    scores = positive_class_scores(model, golden.features)
    metrics = {
        "accuracy": accuracy(golden.labels, y_pred),
        "f1": f1(golden.labels, y_pred),
        "roc_auc": roc_auc(golden.labels, scores),
    }

    now = datetime.now(UTC)
    report: dict[str, object] = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_path": "models/artifacts/fraud_detector/model.pkl",
        "golden_dataset": "evals/golden_dataset.jsonl",
        "golden_dataset_rows": golden.n_rows,
        **metrics,
    }
    report_path = REPORTS_DIR / f"eval_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    write_report(report, report_path)

    print(f"wrote {report_path}")
    print(f"golden_rows={golden.n_rows}")
    for key in ("accuracy", "f1", "roc_auc"):
        print(f"{key}={metrics[key]:.4f}")

    if metrics["accuracy"] < ACCURACY_THRESHOLD:
        print(
            f"ACCURACY GATE FAILED: accuracy {metrics['accuracy']:.4f} < {ACCURACY_THRESHOLD}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"ACCURACY GATE PASSED: accuracy {metrics['accuracy']:.4f} >= {ACCURACY_THRESHOLD}")


if __name__ == "__main__":
    try:
        main()
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
