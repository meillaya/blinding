#!/usr/bin/env bash
# Run all evaluation gates in sequence, failing fast if any gate fails:
#   1. run_eval.py        score the golden dataset, write a report, accuracy gate
#   2. latency_gate.py    p95 in-process predict latency gate (< 100 ms)
#   3. regression_gate.py compare the fresh report against the known-good baseline
# Then wire the gates into the promotion flow (todo 28): flip the evaluated
# version's registry approval (approve_model.py) so `promote_model.py --to prod`
# succeeds. Wired to `make eval`.
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

# --- promotion wiring (todo 28) ----------------------------------------------
# The gates validated the evaluated artifact (eval_common.MODEL_PATH); flip its
# registry record's approved_for_prod so `promote_model.py --to prod` succeeds.
# A version that is not registered yet cannot be approved: warn and stay green
# (register via scripts/upload_model.sh or register_model.py first) — this keeps
# `make eval` standalone-runnable on a fresh clone; any OTHER approval failure
# fails the run.
json_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

eval_meta="models/artifacts/fraud_detector/model_meta.json"
[[ -f "$eval_meta" ]] || { echo "error: evaluated artifact metadata missing: $eval_meta" >&2; exit 1; }
eval_model_id="$(json_field "$eval_meta" model_id)"
eval_model_version="$(json_field "$eval_meta" model_version)"

echo "==> approving ${eval_model_id}@${eval_model_version} for prod (eval gates passed)"
# Capture stderr only (the record JSON on stdout is discarded; one line confirms).
if approve_err="$(uv run python models/registry/approve_model.py \
  --model-id "$eval_model_id" --version "$eval_model_version" 2>&1 >/dev/null)"; then
  echo "approved: ${eval_model_id}@${eval_model_version} approved_for_prod=true (promote --to prod now succeeds)"
elif [[ "$approve_err" == *"not registered"* ]]; then
  echo "WARN: ${eval_model_id}@${eval_model_version} is not registered — approval skipped." >&2
  echo "WARN: register it (scripts/upload_model.sh or models/registry/register_model.py) to enable 'promote --to prod'." >&2
else
  echo "error: approval of ${eval_model_id}@${eval_model_version} failed:" >&2
  echo "$approve_err" >&2
  exit 1
fi
