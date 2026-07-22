#!/usr/bin/env bash
# Run all evaluation gates in sequence, failing fast if any gate fails:
#   1. run_eval.py        score the golden dataset, write a report, accuracy gate
#   2. latency_gate.py    p95 in-process predict latency gate (< 100 ms)
#   3. regression_gate.py compare the fresh report against the known-good baseline
# Wired to `make eval`.
set -euo pipefail

# Resolve the repo root so the gates run identically from any cwd.
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo_root" || exit 1

echo "==> eval gate 1/3: run_eval (accuracy + report)"
uv run python evals/run_eval.py

echo "==> eval gate 2/3: latency_gate (p95 < 100ms)"
uv run python evals/latency_gate.py

echo "==> eval gate 3/3: regression_gate (vs baseline)"
uv run python evals/regression_gate.py

echo "==> all eval gates passed"
