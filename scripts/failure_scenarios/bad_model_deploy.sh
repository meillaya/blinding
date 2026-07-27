#!/usr/bin/env bash
# ============================================================================
# bad_model_deploy.sh — FAILURE SCENARIO 2/4 (todo 49): bad model deploy.
#
# Attempts to ship a deliberately DEGRADED model and proves the PRIMARY MLOps
# defense blocks it BEFORE it can reach the cluster: the eval gate
# (scripts/run_evals.sh, todo 13) runs regression_gate.py, which exits non-zero
# with 'REGRESSION DETECTED' when accuracy OR f1 drops > 3% vs the known-good
# baseline (evals/reports/baseline.json). The degraded artifact never reaches
# register / promote / canary — it is rejected at the gate.
#
# Degraded model: an underfit GradientBoostingClassifier (n_estimators=8,
# max_depth=2, seed from config). Deterministic. It keeps accuracy ABOVE
# run_eval.py's accuracy gate (>= 0.85) but its f1 collapses, so it is the
# REGRESSION gate (not the accuracy gate) that fires — the nuanced, correct gate.
# (A wrong-seed model would fail the accuracy gate first; a majority-class
# predictor would give f1=0 — both less representative. See learnings, todo 31.)
#
# Defense-in-depth (narrated, not re-run here): had the degraded model somehow
# passed the gate and been deployed as a canary, canary_check.sh (todo 30) would
# re-score the candidate artifact vs baseline during the observation window and
# auto-roll-back on the same > 3% drop (fail-closed). The eval gate is the first
# line; the canary eval-check is the second.
#
# Cluster-safe: this scenario performs NO cluster mutation — it is a local eval-
# gate test. It only verifies (read-only) that the cluster is still serving the
# stable model, proving the bad model never reached it. The good artifact is
# backed up and restored by an EXIT trap (idempotent), and the eval report this
# run creates is removed (evals/reports is not gitignored).
#
# Usage:  scripts/failure_scenarios/bad_model_deploy.sh
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
# shellcheck disable=SC1091  # lib path is runtime-resolved (${HERE}); analyse the combined program with `shellcheck -x`
source "${HERE}/_lib.sh"
cd "$ROOT" || exit 1

# Fixed temp paths so the EXIT trap can clean up from ANY phase.
TMP_BASE="${TMPDIR:-/tmp}/inferenceops-t49-badmodel"
MODEL_BACKUP="${TMP_BASE}-model.pkl.bak"
REPORTS_SNAPSHOT="${TMP_BASE}-reports-snapshot"

# cleanup (EXIT trap): restore the good artifact + drop this run's eval reports.
# shellcheck disable=SC2329  # invoked indirectly via `trap cleanup EXIT`
cleanup() {
  set +e
  if [[ -f "$MODEL_BACKUP" ]]; then
    cp "$MODEL_BACKUP" "${STABLE_ARTIFACT_DIR}/model.pkl"
    rm -f "$MODEL_BACKUP"
  fi
  if [[ -f "$REPORTS_SNAPSHOT" ]]; then
    for report in evals/reports/eval_*.json; do
      [[ -e "$report" ]] || continue
      grep -qxF "$report" "$REPORTS_SNAPSHOT" || rm -f "$report"
    done
    rm -f "$REPORTS_SNAPSHOT"
  fi
}
trap cleanup EXIT

require_bins kubectl curl python3 uv
require_cluster
memory_guard

banner "FAILURE SCENARIO 2/4 — bad model deploy → eval gate BLOCKS it (regression_gate.py)"
printf '  Defense 1 (PRIMARY, demonstrated): the eval gate rejects accuracy/f1 drops\n'
printf '  > 3%% vs baseline BEFORE promotion — the model never reaches the cluster.\n'
printf '  Defense 2 (narrated): the canary eval-check (todo 30) auto-rolls-back the\n'
printf '  same > 3%% drop if a degraded model were ever deployed as a canary.\n'

# --- 0. snapshot state for a hermetic cleanup -------------------------------
cp "${STABLE_ARTIFACT_DIR}/model.pkl" "$MODEL_BACKUP"
: > "$REPORTS_SNAPSHOT"
for report in evals/reports/eval_*.json; do
  [[ -e "$report" ]] && printf '%s\n' "$report" >> "$REPORTS_SNAPSHOT"
done

# --- 1. fit a deliberately degraded model into the scored artifact path -----
log "fitting a degraded model (underfit GradientBoostingClassifier) -> ${STABLE_ARTIFACT_DIR}/model.pkl"
uv run python - "$STABLE_ARTIFACT_DIR" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, "models/training")
from dataset import build_dataset, load_config
from sklearn_bridge import GradientBoostingClassifier, dump_model

artifact_dir = Path(sys.argv[1])
config = load_config(Path("models/training/config.yaml"))
splits = build_dataset(config)
weak = GradientBoostingClassifier(
    n_estimators=8, max_depth=2, learning_rate=0.1, random_state=config.seed
)
weak.fit(splits.x_train, splits.y_train)
dump_model(weak, artifact_dir / "model.pkl")
print("wrote degraded artifact (n_estimators=8, max_depth=2)")
PY

# --- 2. run the eval gate — it MUST block the degraded model ----------------
log "running the eval gate on the degraded model (expect REGRESSION DETECTED, non-zero exit)"
EVAL_RC=0
EVAL_OUT="$(scripts/run_evals.sh 2>&1)" || EVAL_RC=$?
printf '%s\n' "$EVAL_OUT"

if [[ "$EVAL_RC" -eq 0 ]]; then
  die "BAD-MODEL defense broken: the eval gate PASSED on a degraded model (expected non-zero exit)"
fi
if [[ "$EVAL_OUT" != *"REGRESSION DETECTED"* ]]; then
  die "BAD-MODEL defense broken: expected 'REGRESSION DETECTED' in eval output (rc=${EVAL_RC})"
fi
ok "BLOCKED: the eval gate exited non-zero (rc=${EVAL_RC}) with REGRESSION DETECTED"

# --- 3. restore the good artifact now (the EXIT trap is a backstop) ---------
cp "$MODEL_BACKUP" "${STABLE_ARTIFACT_DIR}/model.pkl"
rm -f "$MODEL_BACKUP"
log "restored the good stable artifact"

# --- 4. prove the bad model NEVER reached the cluster (read-only) -----------
log "verifying the cluster still serves the stable model (the bad model never deployed)"
if canary_present; then
  die "unexpected canary present — the bad model should never have been deployed"
fi
KEY="$(gateway_api_key)"
RESP="$(predict -H "X-API-Key: ${KEY}")"
[[ "$RESP" == *"\"model_version\":\"${STABLE_VERSION}\""* ]] \
  || die "cluster not serving stable model_version=${STABLE_VERSION} after the blocked deploy: ${RESP}"
ok "cluster untouched: still serving model_version=${STABLE_VERSION} (no canary, no bad deploy)"

verify_stable || die "cluster not stable after bad-model scenario"

ok "SCENARIO PASS: bad_model_deploy — degraded model BLOCKED at the eval gate (REGRESSION DETECTED, rc=${EVAL_RC}); it never reached the cluster (stable ${STABLE_VERSION} still serving)"
printf 'RECOVERY VERIFIED: good artifact restored, eval report cleaned up, cluster still serving model_version=%s, no canary, base ingress + HPA stable.\n' \
  "$STABLE_VERSION"
