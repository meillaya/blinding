"""Regression gate: fail if current metrics drop > 3% vs the known-good baseline.

Standalone: ``uv run python evals/regression_gate.py`` (run ``run_eval.py``
first so a fresh ``evals/reports/eval_*.json`` exists).

Compares the newest ``evals/reports/eval_*.json`` report against the baseline
``evals/reports/baseline.json``. Exits non-zero and prints
``REGRESSION DETECTED`` if accuracy OR f1 drops by more than ``MAX_DROP``
(0.03, absolute) relative to the baseline. roc_auc is shown for context but
is not a gate condition. The canary rollback check (todo 30) reuses this
comparison.
"""

from __future__ import annotations

import sys

from eval_common import (
    BASELINE_PATH,
    EvalError,
    Metrics,
    latest_eval_report,
    read_metrics,
)

MAX_DROP = 0.03  # absolute accuracy/f1 drop vs baseline that fails the gate


def find_regressions(baseline: Metrics, current: Metrics) -> list[str]:
    """Return one message per gated metric that dropped more than MAX_DROP."""
    problems: list[str] = []
    accuracy_drop = baseline.accuracy - current.accuracy
    if accuracy_drop > MAX_DROP:
        problems.append(
            f"accuracy dropped {accuracy_drop:.4f} "
            f"({baseline.accuracy:.4f} -> {current.accuracy:.4f})"
        )
    f1_drop = baseline.f1 - current.f1
    if f1_drop > MAX_DROP:
        problems.append(f"f1 dropped {f1_drop:.4f} ({baseline.f1:.4f} -> {current.f1:.4f})")
    return problems


def main() -> None:
    baseline = read_metrics(BASELINE_PATH)
    current_path = latest_eval_report()
    current = read_metrics(current_path)

    print(
        f"baseline: {BASELINE_PATH} "
        f"(accuracy={baseline.accuracy:.4f} f1={baseline.f1:.4f} roc_auc={baseline.roc_auc:.4f})"
    )
    print(
        f"current:  {current_path} "
        f"(accuracy={current.accuracy:.4f} f1={current.f1:.4f} roc_auc={current.roc_auc:.4f})"
    )

    problems = find_regressions(baseline, current)
    if problems:
        print("REGRESSION DETECTED")
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        raise SystemExit(1)
    print(f"REGRESSION GATE PASSED: no accuracy/f1 drop > {MAX_DROP} vs baseline")


if __name__ == "__main__":
    try:
        main()
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
