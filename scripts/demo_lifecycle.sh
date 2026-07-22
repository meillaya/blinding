#!/usr/bin/env bash
# ============================================================================
# demo_lifecycle.sh — end-to-end MLOps model-lifecycle verification (todo 31).
#
# Drives the FULL promotion lifecycle on the running kind cluster, tying the
# verified components of todos 6, 13, 27, 28, 29, 30 — WITHOUT reimplementing
# any of them (it only CALLS the existing scripts / CLIs / Makefile targets):
#
#   HAPPY PATH — train -> eval -> register -> staging -> canary -> promote:
#     1. TRAIN   (todo 6)  make train (+ evaluate.py for metrics.json)
#     2. EVAL    (todo 13) scripts/run_evals.sh — 3 gates (accuracy/latency/
#                           regression) + the approve_model.py wiring
#     3. REGISTER(todo 27/28) scripts/upload_model.sh — S3 upload (moto) +
#                           register_model.py (candidate 2.0.0, unapproved)
#     4. STAGING (todo 27) promote_model.py --to staging (+ a prod-gate demo:
#                           --to prod is REJECTED until approve_model.py flips
#                           approved_for_prod, then it succeeds)
#     5. CANARY  (todo 29) make deploy-canary MODEL_VERSION=2.0.0 — 90/10 split
#     6. OBSERVE (todo 30) sustained traffic + canary_check.sh loop reaches
#                           'CANARY HEALTHY -> promote-ready' (NEVER auto-promotes)
#     7. PROMOTE (todo 29) make promote — deliberate 100% candidate; verified via
#                           the model_version label in the live /v1/predict response
#     8. RESTORE           make rollback — base ingress + stable model reinstated
#
#   BAD-MODEL PATH — the primary defense:
#     A deliberately DEGRADED artifact (an underfit GradientBoostingClassifier)
#     is BLOCKED at the eval gate: scripts/run_evals.sh's regression_gate.py exits
#     non-zero with 'REGRESSION DETECTED' (f1 drop > 3% vs baseline) — BEFORE any
#     promotion. The canary eval-check auto-rollback (todo 30) is narrated as the
#     SECOND line of defense (a degraded model that somehow passed the gate would
#     be rolled back during the canary observation window).
#
# CODE-VERSION vs MODEL-VERSION (prior-plan M9): the git semver/SHA of THIS repo
# (the code that trains/serves) is DECOUPLED from the model-version — an opaque
# registry label (1.0.0 stable, 2.0.0 candidate). One code revision can ship many
# model-versions; the registry keys on <model_id>@<model_version>, never git semver.
#
# Demo-speed knobs: the canary observation uses CANARY_HEALTHY_THRESHOLD=2 and
# CANARY_CHECK_INTERVAL=10 (overridable via env). PRODUCTION DEFAULTS are
# CANARY_HEALTHY_THRESHOLD=3 / CANARY_CHECK_INTERVAL=30 (see canary_check.sh /
# deploy_canary.sh).
#
# Idempotent + self-cleaning: an EXIT trap stops the traffic generator and the
# moto server, runs `make rollback` (idempotent), restores the stable artifact,
# removes the temp registry/model-card/eval-reports this run created, and leaves
# the cluster at base (ingress -> service-gateway, stable model serving, no orphan
# canary, no check loop, Prometheus untouched).
#
# QA records: /tmp/qa/t31_lifecycle.txt (happy) + /tmp/qa/t31_lifecycle_fail.txt
# (bad model blocked).
#
# Usage:  scripts/demo_lifecycle.sh
# ============================================================================
set -euo pipefail

# --- configuration ----------------------------------------------------------
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1000/docker-me.sock}"

K8S_CONTEXT="${KUBE_CONTEXT:-kind-inferenceops}"
NAMESPACE="${NAMESPACE:-inferenceops}"
INGRESS_HOST="inferenceops.local"
INGRESS_PORT="${INGRESS_PORT:-8080}"

MODEL_ID="fraud-detector"
STABLE_VERSION="1.0.0"
CANDIDATE_VERSION="2.0.0"

STABLE_ARTIFACT_DIR="models/artifacts/fraud_detector"
CANARY_ARTIFACT_DIR="models/artifacts/fraud_detector_canary"

# Demo-speed canary observation (PRODUCTION DEFAULTS: 3 / 30s). Overridable.
export CANARY_HEALTHY_THRESHOLD="${CANARY_HEALTHY_THRESHOLD:-2}"
export CANARY_CHECK_INTERVAL="${CANARY_CHECK_INTERVAL:-10}"
PROMOTE_READY_TIMEOUT="${PROMOTE_READY_TIMEOUT:-300}"

# moto (S3 stub) for the todo-28 upload — on a dedicated port to avoid clashes.
MOTO_PORT="${MOTO_PORT:-5011}"
MOTO_ENDPOINT="http://127.0.0.1:${MOTO_PORT}"
MODEL_BUCKET="inferenceops-lifecycle-demo"
# moto accepts any credentials; static fake keys keep boto3 offline.
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-testing}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-testing}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

# Fixed temp/state paths so the EXIT trap can clean up from ANY phase.
TMP_BASE="${TMPDIR:-/tmp}/inferenceops-demo"
TRAFFIC_PIDFILE="${TMP_BASE}-traffic.pid"
MOTO_PIDFILE="${TMP_BASE}-moto.pid"
REGISTRY_FILE="${TMP_BASE}-registry.json"
MODEL_CARD_OUT="${TMP_BASE}-card.md"
MODEL_BACKUP="${TMP_BASE}-model.pkl.bak"
REPORTS_SNAPSHOT="${TMP_BASE}-reports-snapshot"
QA_DIR="/tmp/qa"
QA_HAPPY="${QA_DIR}/t31_lifecycle.txt"
QA_FAIL="${QA_DIR}/t31_lifecycle_fail.txt"

# Shared with deploy_canary.sh / canary_check.sh / promote.sh / rollback.sh:
# canary_check.sh writes this marker after CANARY_HEALTHY_THRESHOLD consecutive
# healthy checks (it NEVER auto-promotes); we poll it to know when to promote.
CANARY_READY_MARKER="${TMPDIR:-/tmp}/inferenceops-canary-promote-ready"

# 20-feature fraud-detector predict body (probe payload only).
PREDICT_BODY='{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'

# --- locate repo root (script lives in <root>/scripts) ----------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- helpers ----------------------------------------------------------------
log()    { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
banner() { printf '\n\033[1;35m==== %s ====\033[0m\n' "$*"; }
die()    { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# json_field <file> <key> — read a top-level string field from a JSON object.
json_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

# CODE-VERSION (git semver/SHA) — decoupled from the model-version labels.
code_version() {
  git describe --tags --always 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo "unknown"
}

# gateway_api_key — read the live gateway's first API key from the running pod
# env (the deployed key, not the example secret), for post-rollback verification.
gateway_api_key() {
  kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" exec deploy/gateway \
    -- printenv API_KEYS 2>/dev/null | cut -d, -f1
}

# predict <extra-curl-args...> — POST /v1/predict through the ingress host.
# During the canary the gateway is bypassed, so callers pass only the Host header.
predict() {
  curl -s --max-time 5 -X POST "http://localhost:${INGRESS_PORT}/v1/predict" \
    -H "Host: ${INGRESS_HOST}" -H 'Content-Type: application/json' \
    -d "$PREDICT_BODY" "$@" 2>/dev/null || true
}

# --- background traffic generator (canary observation window) ---------------
# Sustained ~5 req/s against /v1/predict (Host header only — the gateway is
# bypassed during the canary) so BOTH the stable (90%) and candidate (10%) tiers
# emit model_version-filtered metrics; without traffic the candidate p95 decays
# out of canary_check.sh's 1m rate window and it reports 'CANARY WARMING UP'
# forever. setsid -> own session/process group, so `kill -- -PID` tears down the
# whole loop (+ in-flight curl); the sentinel arg lets the backstop pkill find it.
start_traffic() {
  stop_traffic
  # shellcheck disable=SC2016  # inner single-quoted script: $body expands in the loop's bash, not here
  setsid bash -c '
    body="$1"
    while true; do
      curl -s -o /dev/null --max-time 2 -X POST "http://localhost:8080/v1/predict" \
        -H "Host: inferenceops.local" -H "Content-Type: application/json" \
        -d "$body" >/dev/null 2>&1 || true
      sleep 0.2
    done
  ' inferenceopsDemoTrafficLoop "$PREDICT_BODY" >/dev/null 2>&1 &
  echo "$!" > "$TRAFFIC_PIDFILE"
  log "traffic generator started (~5 req/s; pid $(cat "$TRAFFIC_PIDFILE"))"
}

stop_traffic() {
  if [[ -f "$TRAFFIC_PIDFILE" ]]; then
    local pid
    pid="$(cat "$TRAFFIC_PIDFILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill -- "-${pid}" 2>/dev/null || kill "$pid" 2>/dev/null || true
    fi
    rm -f "$TRAFFIC_PIDFILE"
  fi
  # Backstop: kill any surviving loop by its sentinel (char-class avoids self-match).
  pkill -f "inferenceopsDemoTrafficLo[o]p" 2>/dev/null || true
}

# --- moto (S3 stub) lifecycle -----------------------------------------------
start_moto() {
  stop_moto
  if ss -ltn 2>/dev/null | grep -q ":${MOTO_PORT} "; then
    die "port ${MOTO_PORT} already in use — cannot start moto (set MOTO_PORT to a free port)"
  fi
  setsid uv run python -m moto.server -p "$MOTO_PORT" >/dev/null 2>&1 &
  echo "$!" > "$MOTO_PIDFILE"
  local _
  for _ in $(seq 1 60); do
    if curl -s --max-time 2 "$MOTO_ENDPOINT" >/dev/null 2>&1; then
      log "moto S3 stub ready at ${MOTO_ENDPOINT} (pid $(cat "$MOTO_PIDFILE"))"
      return 0
    fi
    sleep 0.5
  done
  die "moto did not become ready at ${MOTO_ENDPOINT} within 30s"
}

stop_moto() {
  if [[ -f "$MOTO_PIDFILE" ]]; then
    local pid
    pid="$(cat "$MOTO_PIDFILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]]; then
      kill -- "-${pid}" 2>/dev/null || kill "$pid" 2>/dev/null || true
    fi
    rm -f "$MOTO_PIDFILE"
  fi
  # Backstop: kill any surviving moto server (char-class avoids self-match).
  pkill -f "moto[.]server" 2>/dev/null || true
}

# create_bucket — create the demo bucket on the moto endpoint (boto3).
create_bucket() {
  uv run python - "$MOTO_ENDPOINT" "$MODEL_BUCKET" <<'PY'
import sys
import boto3

endpoint, bucket = sys.argv[1], sys.argv[2]
client = boto3.client("s3", endpoint_url=endpoint)
client.create_bucket(Bucket=bucket)
print(f"bucket ready: s3://{bucket}")
PY
}

# --- registry CLIs (todo 27) against the temp local registry ----------------
registry_register() {  # done by upload_model.sh (S3 upload + register)
  ARTIFACT_DIR="$CANARY_ARTIFACT_DIR" \
  MODEL_BUCKET="$MODEL_BUCKET" \
  S3_ENDPOINT_URL="$MOTO_ENDPOINT" \
  REGISTRY_FILE="$REGISTRY_FILE" \
  MODEL_CARD_OUT="$MODEL_CARD_OUT" \
  scripts/upload_model.sh
}

registry_promote() {  # <target: staging|prod>
  uv run python models/registry/promote_model.py \
    --model-id "$MODEL_ID" --version "$CANDIDATE_VERSION" \
    --to "$1" --registry-file "$REGISTRY_FILE" --model-card-out "$MODEL_CARD_OUT"
}

registry_approve() {
  uv run python models/registry/approve_model.py \
    --model-id "$MODEL_ID" --version "$CANDIDATE_VERSION" \
    --registry-file "$REGISTRY_FILE" --model-card-out "$MODEL_CARD_OUT"
}

# --- cleanup (EXIT trap): leave the cluster + repo clean --------------------
cleanup() {
  set +e
  printf '\n\033[1;33m---- cleanup: restoring stable state ----\033[0m\n'
  stop_traffic
  stop_moto
  # Idempotent: removes any canary, stops the check loop, restores base ingress.
  scripts/rollback.sh >/dev/null 2>&1
  # Restore the stable artifact if the bad-model path overwrote it.
  if [[ -f "$MODEL_BACKUP" ]]; then
    cp "$MODEL_BACKUP" "${STABLE_ARTIFACT_DIR}/model.pkl"
    rm -f "$MODEL_BACKUP"
  fi
  # Remove eval reports THIS run created (restore the pre-run reports dir).
  if [[ -f "$REPORTS_SNAPSHOT" ]]; then
    for report in evals/reports/eval_*.json; do
      [[ -e "$report" ]] || continue
      grep -qxF "$report" "$REPORTS_SNAPSHOT" || rm -f "$report"
    done
    rm -f "$REPORTS_SNAPSHOT"
  fi
  rm -f "$REGISTRY_FILE" "$MODEL_CARD_OUT" "$TRAFFIC_PIDFILE" "$MOTO_PIDFILE"
  printf '\033[1;33m---- cleanup done ----\033[0m\n'
}
trap cleanup EXIT

# --- preflight --------------------------------------------------------------
preflight() {
  for bin in docker kind kubectl python3 curl jq; do
    command -v "$bin" >/dev/null 2>&1 || die "required binary not found on PATH: $bin"
  done
  kubectl --context "$K8S_CONTEXT" get ns "$NAMESPACE" >/dev/null 2>&1 \
    || die "namespace ${NAMESPACE} not found on context ${K8S_CONTEXT} (run scripts/deploy_local.sh first)"
  mkdir -p "$QA_DIR"
  # Snapshot the eval reports dir so cleanup removes only what this run creates.
  : > "$REPORTS_SNAPSHOT"
  for report in evals/reports/eval_*.json; do
    [[ -e "$report" ]] && printf '%s\n' "$report" >> "$REPORTS_SNAPSHOT"
  done
}

# ============================================================================
# HAPPY PATH
# ============================================================================
happy_path() {
  banner "HAPPY PATH — full MLOps model lifecycle (train→eval→register→staging→canary→promote→rollback)"

  log "VERSIONING NOTE (prior-plan M9): code-version vs model-version are DECOUPLED"
  printf '  code-version  (git semver/SHA of this repo): %s\n' "$(code_version)"
  printf '  model-version (opaque registry label):       %s (stable) / %s (candidate)\n' \
    "$STABLE_VERSION" "$CANDIDATE_VERSION"
  printf '  The registry keys on <model_id>@<model_version> = %s@%s — NEVER git semver.\n' \
    "$MODEL_ID" "$CANDIDATE_VERSION"
  printf '  One code revision can ship many model-versions; re-training bumps the\n'
  printf '  model-version, not the git tag.\n'

  # --- STEP 1: TRAIN (todo 6) ----------------------------------------------
  log "STEP 1/8 — TRAIN (todo 6): make train (+ evaluate.py for metrics.json)"
  make train
  uv run python models/training/evaluate.py
  # Materialize the candidate artifact: the freshly trained weights under the NEW
  # model-version label (2.0.0). Its committed model_meta.json carries 2.0.0; only
  # the (gitignored) model.pkl is refreshed. A real canary would carry a genuinely
  # retrained artifact here — same mechanism, different weights.
  cp "${STABLE_ARTIFACT_DIR}/model.pkl" "${CANARY_ARTIFACT_DIR}/model.pkl"
  printf '  trained: %s@%s (accuracy=%s f1=%s)\n' \
    "$(json_field "${STABLE_ARTIFACT_DIR}/model_meta.json" model_id)" \
    "$(json_field "${STABLE_ARTIFACT_DIR}/model_meta.json" model_version)" \
    "$(json_field "${STABLE_ARTIFACT_DIR}/metrics.json" accuracy)" \
    "$(json_field "${STABLE_ARTIFACT_DIR}/metrics.json" f1)"
  printf '  candidate materialized: %s@%s (freshly trained weights, new version label)\n' \
    "$MODEL_ID" "$CANDIDATE_VERSION"

  # --- STEP 2: EVAL GATES (todo 13) ----------------------------------------
  log "STEP 2/8 — EVAL GATES (todo 13): scripts/run_evals.sh (accuracy + latency + regression + approval wiring)"
  scripts/run_evals.sh
  printf '  the 3 gates validated the trained weights vs evals/reports/baseline.json.\n'
  printf '  run_evals.sh then calls approve_model.py for the EVALUATED version — the\n'
  printf '  PRIMARY promotion gate. (The regression gate is what blocks bad models —\n'
  printf '  demonstrated in the BAD-MODEL path.)\n'

  # --- STEP 3: REGISTER (todo 27/28) ---------------------------------------
  log "STEP 3/8 — REGISTER (todo 27/28): upload_model.sh (S3 upload via moto + register_model.py)"
  start_moto
  create_bucket
  registry_register
  printf '  registered %s@%s with an s3:// artifact_uri, approved_for_prod=false.\n' \
    "$MODEL_ID" "$CANDIDATE_VERSION"
  printf '  model-version %s is an OPAQUE registry label — unrelated to git semver (%s).\n' \
    "$CANDIDATE_VERSION" "$(code_version)"

  # --- STEP 4: STAGING + prod-gate demo (todo 27) --------------------------
  log "STEP 4/8 — PROMOTE TO STAGING (todo 27) + prod-gate demonstration"
  registry_promote staging >/dev/null
  printf '  promoted %s@%s -> status=staging (staging needs no approval).\n' \
    "$MODEL_ID" "$CANDIDATE_VERSION"
  # Prod gate: --to prod is REJECTED while approved_for_prod=false ...
  printf '  prod-gate demo: promote --to prod while unapproved (expect REJECTION):\n'
  if prod_reject="$(registry_promote prod 2>&1)"; then
    die "prod-gate broken: promote --to prod SUCCEEDED on an unapproved version"
  fi
  printf '    rejected as expected: %s\n' "$prod_reject"
  # ... approve_model.py flips approved_for_prod (the eval-gate wiring's job) ...
  registry_approve >/dev/null
  printf '  approved %s@%s: approved_for_prod=true (this is what run_evals.sh does\n' \
    "$MODEL_ID" "$CANDIDATE_VERSION"
  printf '  after the gates pass; invoked directly here since the eval gate approved\n'
  printf '  the evaluated weights under their own version label).\n'
  # ... and now --to prod succeeds.
  registry_promote prod >/dev/null
  printf '  promoted %s@%s -> status=prod (gate unlocked by approval).\n' \
    "$MODEL_ID" "$CANDIDATE_VERSION"
  stop_moto

  # --- STEP 5: DEPLOY CANARY (todo 29) -------------------------------------
  log "STEP 5/8 — DEPLOY CANARY (todo 29): make deploy-canary MODEL_VERSION=${CANDIDATE_VERSION} (90/10 split)"
  printf '  canary observation uses CANARY_HEALTHY_THRESHOLD=%s / CANARY_CHECK_INTERVAL=%ss\n' \
    "$CANARY_HEALTHY_THRESHOLD" "$CANARY_CHECK_INTERVAL"
  printf '  (PRODUCTION DEFAULTS: 3 / 30s).\n'
  make deploy-canary "MODEL_VERSION=${CANDIDATE_VERSION}"

  # --- STEP 6: CANARY OBSERVATION (todo 30) --------------------------------
  log "STEP 6/8 — CANARY OBSERVATION (todo 30): sustained traffic + canary_check.sh loop -> promote-ready"
  start_traffic
  printf '  waiting (<= %ss) for promote-ready (threshold=%s consecutive healthy, every %ss)\n' \
    "$PROMOTE_READY_TIMEOUT" "$CANARY_HEALTHY_THRESHOLD" "$CANARY_CHECK_INTERVAL"
  local waited=0
  while [[ ! -f "$CANARY_READY_MARKER" ]]; do
    if [[ "$waited" -ge "$PROMOTE_READY_TIMEOUT" ]]; then
      tail -40 "${TMPDIR:-/tmp}/inferenceops-canary-check.log" 2>/dev/null || true
      die "canary did not reach promote-ready within ${PROMOTE_READY_TIMEOUT}s"
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  printf '\n\033[1;32m  promote-ready marker present — canary observation window passed.\033[0m\n'
  printf '  canary_check.sh reported CANARY HEALTHY -> promote-ready; it NEVER auto-promotes.\n'
  printf '  (Recent check log:)\n'
  tail -12 "${TMPDIR:-/tmp}/inferenceops-canary-check.log" 2>/dev/null || true

  # --- STEP 7: DELIBERATE PROMOTE (todo 29) --------------------------------
  log "STEP 7/8 — DELIBERATE PROMOTE (todo 29): make promote -> 100% candidate"
  make promote
  # make promote sets canary-weight=100 and probes until it FIRST sees the candidate,
  # but the nginx data plane reconciles across its replicas over a few seconds — a
  # request in that window can still hit a not-yet-reloaded replica at weight=10.
  # So re-verify with a bounded wait for CONSECUTIVE candidate responses (proves
  # EVERY replica now routes 100% to the candidate), discriminated by model_version.
  printf '  verifying live traffic serves the candidate (model_version=%s):\n' "$CANDIDATE_VERSION"
  local resp consecutive=0 probes=0 need=5
  while [[ "$consecutive" -lt "$need" && "$probes" -lt 40 ]]; do
    resp="$(predict)"
    printf '    %s\n' "$resp"
    if [[ "$resp" == *"\"model_version\":\"${CANDIDATE_VERSION}\""* ]]; then
      consecutive=$(( consecutive + 1 ))
    else
      consecutive=0
    fi
    probes=$(( probes + 1 ))
    sleep 1
  done
  [[ "$consecutive" -ge "$need" ]] \
    || die "post-promote traffic not stably serving candidate model_version=${CANDIDATE_VERSION} after ${probes} probes"
  printf '  verified: %s consecutive responses served model_version=%s (100%% candidate on %s).\n' \
    "$need" "$CANDIDATE_VERSION" "$INGRESS_HOST"
  stop_traffic

  # --- STEP 8: RESTORE TO STABLE -------------------------------------------
  log "STEP 8/8 — RESTORE TO STABLE: make rollback (base ingress + stable model)"
  make rollback
  # Reconcile race (mirror of promote): until nginx drops the deleted canary route,
  # a TERMINATING canary pod can still serve the candidate via the stale route —
  # so require CONSECUTIVE stable responses (base path reconciled + canary drained).
  # The gateway is reinstated on the base path, so the probe needs X-API-Key.
  local key resp2
  key="$(gateway_api_key)"
  printf '  verifying stable serving via the reinstated gateway (model_version=%s):\n' "$STABLE_VERSION"
  consecutive=0; probes=0
  while [[ "$consecutive" -lt "$need" && "$probes" -lt 40 ]]; do
    resp2="$(predict -H "X-API-Key: ${key}")"
    printf '    %s\n' "$resp2"
    if [[ "$resp2" == *"\"model_version\":\"${STABLE_VERSION}\""* ]]; then
      consecutive=$(( consecutive + 1 ))
    else
      consecutive=0
    fi
    probes=$(( probes + 1 ))
    sleep 1
  done
  [[ "$consecutive" -ge "$need" ]] \
    || die "post-rollback traffic not stably serving stable model_version=${STABLE_VERSION} after ${probes} probes"
  printf '  verified: %s consecutive responses served model_version=%s (base path restored: ingress -> service-gateway -> stable %s@%s).\n' \
    "$need" "$STABLE_VERSION" "$MODEL_ID" "$STABLE_VERSION"

  banner "HAPPY PATH COMPLETE — lifecycle reached promote-ready, deliberately promoted to 100% candidate, then restored to stable"
}

# ============================================================================
# BAD-MODEL PATH — blocked at the eval gate (primary defense)
# ============================================================================
bad_model_path() {
  banner "BAD-MODEL PATH — a degraded model is BLOCKED at the eval gate (regression_gate.py)"

  printf '  Defense-in-depth for model quality:\n'
  printf '    1. PRIMARY  — the eval gate (scripts/run_evals.sh): regression_gate.py\n'
  printf '       rejects accuracy/f1 drops > 3%% vs baseline BEFORE promotion.\n'
  printf '    2. SECOND   — the canary eval-check auto-rollback (todo 30): if a degraded\n'
  printf '       model somehow passed the gate and was deployed as a canary,\n'
  printf '       canary_check.sh re-scores the candidate artifact vs baseline and\n'
  printf '       auto-rolls-back on a > 3%% drop (fail-closed).\n'
  printf '  This path demonstrates the PRIMARY defense.\n'

  # Back up the good stable artifact (restored by the EXIT trap + explicitly here).
  cp "${STABLE_ARTIFACT_DIR}/model.pkl" "$MODEL_BACKUP"

  # Fit a deliberately DEGRADED model: an underfit GradientBoostingClassifier
  # (8 estimators, depth 2). Deterministic (seed 42). It keeps accuracy above the
  # run_eval.py gate (>= 0.85) but its f1 collapses, so the REGRESSION gate fires.
  log "fitting a deliberately degraded model (underfit GradientBoostingClassifier) -> ${STABLE_ARTIFACT_DIR}/model.pkl"
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

  # Run the eval gate — it MUST fail (regression detected) and exit non-zero.
  log "running the eval gate on the degraded model (expect REGRESSION DETECTED, non-zero exit)"
  local eval_rc=0 eval_output=""
  eval_output="$(scripts/run_evals.sh 2>&1)" || eval_rc=$?
  printf '%s\n' "$eval_output"

  if [[ "$eval_rc" -eq 0 ]]; then
    die "BAD-MODEL path broken: the eval gate PASSED on a degraded model (expected non-zero exit)"
  fi
  if [[ "$eval_output" != *"REGRESSION DETECTED"* ]]; then
    die "BAD-MODEL path broken: expected 'REGRESSION DETECTED' in eval output (rc=${eval_rc})"
  fi

  printf '\n\033[1;32m  BLOCKED: the eval gate exited non-zero (rc=%s) with REGRESSION DETECTED.\033[0m\n' "$eval_rc"
  printf '  The degraded model was rejected BEFORE register/promote/canary — it never\n'
  printf '  reached the cluster. This is the primary MLOps defense.\n'
  printf '  Had it slipped through, the canary eval-check (todo 30) would have caught\n'
  printf '  the same > 3%% f1 drop during the observation window and auto-rolled-back.\n'

  # Restore the good artifact now (the EXIT trap is a backstop).
  cp "$MODEL_BACKUP" "${STABLE_ARTIFACT_DIR}/model.pkl"
  rm -f "$MODEL_BACKUP"

  banner "BAD-MODEL PATH COMPLETE — degraded model blocked at the eval gate (REGRESSION DETECTED, non-zero exit)"
}

# --- run a phase with all output tee'd to a QA file + console ---------------
# Keeps the phase in the CURRENT shell (no subshell), so set -e and the EXIT trap
# behave normally; `|| rc=$?` captures failure without tripping set -e, restores
# the console fds, and returns the status for the caller to handle.
run_captured() {  # <qa-file> <fn>
  local qa="$1" fn="$2" rc=0
  : > "$qa"
  exec 3>&1 4>&2
  exec > >(tee -a "$qa") 2>&1
  "$fn" || rc=$?
  exec >&3 2>&4
  exec 3>&- 4>&-
  sleep 1  # let tee flush before the next phase / exit
  return "$rc"
}

# --- main -------------------------------------------------------------------
preflight

if ! run_captured "$QA_HAPPY" happy_path; then
  die "HAPPY PATH failed (see ${QA_HAPPY})"
fi

if ! run_captured "$QA_FAIL" bad_model_path; then
  die "BAD-MODEL PATH failed (see ${QA_FAIL})"
fi

banner "LIFECYCLE VERIFICATION COMPLETE — records: ${QA_HAPPY} + ${QA_FAIL}"
