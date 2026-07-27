#!/usr/bin/env bash
# ============================================================================
# latency_regression.sh — FAILURE SCENARIO 3/4 (todo 49): latency regression.
#
# Deploys a canary (model_version=2.0.0) whose candidate shows a p95 LATENCY
# REGRESSION, and proves the automated canary health check (scripts/canary_check.sh,
# todo 30) detects it and AUTO-ROLLS-BACK: candidate p95 > stable p95 x 1.20 ->
# 'ROLLBACK TRIGGERED' -> scripts/rollback.sh removes the canary + restores the
# base ingress (-> service-gateway). The stable model-server is never touched.
#
# Deterministic injection (the todo-30 QA technique): rather than waiting for a
# genuinely slow candidate to emit metrics through a Prometheus scrape window, we
# point canary_check.sh at a synthetic Prometheus query API (PROM_BASE_URL) served
# by prom_latency_stub.py. The stub returns a HIGH p95 for the candidate's
# model_version-filtered histogram_quantile query and a LOW p95 for the stable
# version, so the +20% breach is reproducible and immediate. canary_check.sh's
# p95 check (check 1) fires FIRST, so the rollback triggers before the (passing)
# eval check — exactly the spec §M8 latency trigger.
#
# Sequence: deploy canary -> STOP the automated check loop (so only our stub-driven
# check runs — deterministic) -> start the stub -> run canary_check.sh against it
# -> ROLLBACK TRIGGERED -> verify canary gone + base ingress restored -> kill stub.
#
# Cluster-stable-aware: pre-flight memory guard; the EXIT trap kills the stub and
# runs an idempotent `scripts/rollback.sh` (removes any leftover canary, restores
# the base ingress), so the cluster is left at base from ANY phase.
#
# Usage:  scripts/failure_scenarios/latency_regression.sh
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
# shellcheck disable=SC1091  # lib path is runtime-resolved (${HERE}); analyse the combined program with `shellcheck -x`
source "${HERE}/_lib.sh"
cd "$ROOT" || exit 1

STUB_PY="${HERE}/prom_latency_stub.py"
TMP_BASE="${TMPDIR:-/tmp}/inferenceops-t49-latency"
STUB_PORT_FILE="${TMP_BASE}-stub.port"
STUB_LOG="${TMP_BASE}-stub.log"
STUB_PID=""

# Synthetic p95 values: candidate 0.900s vs stable 0.100s -> ratio 9.0 > 1.20.
STUB_CAND_P95="${STUB_CAND_P95:-0.9}"
STUB_STABLE_P95="${STUB_STABLE_P95:-0.1}"

# Shared with deploy_canary.sh / canary_check.sh / rollback.sh (todo 30).
CANARY_LOOP_PIDFILE="${TMPDIR:-/tmp}/inferenceops-canary-check.pid"

# cleanup (EXIT trap): kill the stub + idempotently restore the base cluster.
# shellcheck disable=SC2329  # invoked indirectly via `trap cleanup EXIT`
cleanup() {
  set +e
  if [[ -n "$STUB_PID" ]]; then
    kill -- "-${STUB_PID}" 2>/dev/null || kill "${STUB_PID}" 2>/dev/null || true
  fi
  rm -f "$STUB_PORT_FILE" "$STUB_LOG"
  restore_base
}
trap cleanup EXIT

require_bins docker kind kubectl python3 curl
require_cluster
memory_guard
[[ -f "$STUB_PY" ]] || die "stub server missing: ${STUB_PY}"

banner "FAILURE SCENARIO 3/4 — latency regression → canary_check.sh AUTO-ROLLBACK"
printf '  Trigger (spec §M8): candidate p95 > stable p95 x 1.20.\n'
printf '  Injected deterministically via a PROM_BASE_URL stub (candidate %ss vs stable %ss).\n' \
  "$STUB_CAND_P95" "$STUB_STABLE_P95"

# --- 1. deploy the canary (starts the automated check loop) -----------------
# A long check interval keeps the loop quiet; we stop it outright in step 2 so only
# our stub-driven check runs (deterministic — no race with a real Prometheus read).
log "deploying canary model_version=${CANDIDATE_VERSION} (90/10 split)"
CANARY_CHECK_INTERVAL=600 scripts/deploy_canary.sh "$CANDIDATE_VERSION"

# --- 2. stop the automated check loop (canary stays up) ---------------------
if [[ -f "$CANARY_LOOP_PIDFILE" ]]; then
  LOOP_PID="$(cat "$CANARY_LOOP_PIDFILE" 2>/dev/null || true)"
  if [[ -n "$LOOP_PID" ]]; then
    log "stopping the automated check loop (pid ${LOOP_PID}) — only our stub check will run"
    kill -- "-${LOOP_PID}" 2>/dev/null || kill "${LOOP_PID}" 2>/dev/null || true
  fi
  rm -f "$CANARY_LOOP_PIDFILE"
fi

# --- 3. resolve the candidate/stable versions canary_check.sh will use ------
CAND_VER="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get deployment model-server-canary \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="MODEL_VERSION")].value}' 2>/dev/null || true)"
CAND_VER="${CAND_VER:-$CANDIDATE_VERSION}"
STABLE_VER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["model_version"])' \
  "${STABLE_ARTIFACT_DIR}/model_meta.json" 2>/dev/null || echo "$STABLE_VERSION")"
log "canary_check.sh will compare candidate=${CAND_VER} vs stable=${STABLE_VER}"

# --- 4. start the synthetic Prometheus stub ---------------------------------
log "starting the PROM_BASE_URL stub (prom_latency_stub.py)"
rm -f "$STUB_PORT_FILE"
STUB_CAND_VER="$CAND_VER" STUB_STABLE_VER="$STABLE_VER" \
STUB_CAND_P95="$STUB_CAND_P95" STUB_STABLE_P95="$STUB_STABLE_P95" \
STUB_PORT_FILE="$STUB_PORT_FILE" \
  setsid python3 "$STUB_PY" >"$STUB_LOG" 2>&1 &
STUB_PID=$!

STUB_PORT=""
for _ in $(seq 1 40); do
  [[ -s "$STUB_PORT_FILE" ]] && STUB_PORT="$(cat "$STUB_PORT_FILE" 2>/dev/null || true)"
  [[ -n "$STUB_PORT" ]] && break
  sleep 0.25
done
[[ -n "$STUB_PORT" ]] || { cat "$STUB_LOG" >&2 || true; die "stub did not report a port"; }
PROM_BASE_URL="http://127.0.0.1:${STUB_PORT}"
curl -sS --max-time 5 "${PROM_BASE_URL}/-/ready" >/dev/null 2>&1 \
  || { cat "$STUB_LOG" >&2 || true; die "stub /-/ready failed at ${PROM_BASE_URL}"; }
ok "stub ready at ${PROM_BASE_URL} (candidate p95=${STUB_CAND_P95}s, stable p95=${STUB_STABLE_P95}s)"

# --- 5. run canary_check.sh against the stub -> expect auto-rollback --------
log "running canary_check.sh against the stub (expect ROLLBACK TRIGGERED)"
CHECK_RC=0
CHECK_OUT="$(PROM_BASE_URL="$PROM_BASE_URL" scripts/canary_check.sh 2>&1)" || CHECK_RC=$?
printf '%s\n' "$CHECK_OUT"

if [[ "$CHECK_OUT" != *"ROLLBACK TRIGGERED"* ]]; then
  die "latency-regression defense broken: no 'ROLLBACK TRIGGERED' in canary_check.sh output (rc=${CHECK_RC})"
fi
if [[ "$CHECK_OUT" != *"p95 regression"* ]]; then
  die "latency-regression defense broken: rollback was not triggered by the p95 check (rc=${CHECK_RC})"
fi
ok "ROLLBACK TRIGGERED by the p95 check (candidate ${STUB_CAND_P95}s > stable ${STUB_STABLE_P95}s x 1.20)"

# --- 6. verify the canary is gone + base ingress restored -------------------
if canary_present; then
  die "canary deployment still present after rollback"
fi
BACKEND="$(ingress_backend)"
[[ "$BACKEND" == "service-gateway" ]] \
  || die "ingress backend is '${BACKEND}', expected service-gateway after rollback"
ok "canary removed + base ingress restored (-> service-gateway)"

verify_stable || die "cluster not stable after latency-regression scenario"

ok "SCENARIO PASS: latency_regression — canary_check.sh detected candidate p95 ${STUB_CAND_P95}s > stable ${STUB_STABLE_P95}s x 1.20, AUTO-ROLLED-BACK, canary removed, base ingress restored"
printf 'RECOVERY VERIFIED: canary (model_version=%s) removed, ingress->service-gateway, stable %s serving, HPA stable.\n' \
  "$CAND_VER" "$STABLE_VERSION"
