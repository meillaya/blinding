#!/usr/bin/env bash
# ============================================================================
# canary_check.sh — automated canary health check + auto-rollback (todo 30).
# Wired to `make canary-check`, and run on a 30s interval by the loop that
# scripts/deploy_canary.sh starts (Oracle S9). Compares the CANDIDATE canary
# against the STABLE baseline, discriminated by the `model_version` metric
# label (Oracle S1, todo 8), querying the in-cluster kube-prometheus-stack
# Prometheus (todo 19).
#
# Checks (spec §M8 rollback triggers):
#   1. p95 latency per version (histogram_quantile on
#      inferenceops_request_duration_seconds_bucket{model_version="<v>"}):
#      ROLLBACK if candidate p95 > stable p95 × 1.20 (+20%).
#   2. error rate per version (rate(inferenceops_error_count_total{...}) over
#      rate(inferenceops_request_count_total{...})): ROLLBACK if candidate
#      error ratio > 1%.
#   3. eval score: score the candidate artifact on the golden dataset and
#      compare against evals/reports/baseline.json: ROLLBACK if accuracy OR f1
#      drops > 3%.
#   4. GPU mem (DCGM_FI_DEV_FB_USED) — INFORMATIONAL only; skipped gracefully
#      when no GPU/DCGM metrics are present. Never triggers a rollback.
#
# FAIL-CLOSED (Oracle S8): ANY unexpected error or ambiguity — script bug,
# malformed/empty query result where data is expected, `make rollback` failure
# — converges to a conservative rollback to last-known-good, the SAME path as
# Prometheus-unreachable. The script never fails open.
#
# Prometheus-down fallback: if Prometheus is unreachable for > 60s (grace
# window tracked across iterations) -> conservative rollback. A transient blip
# within the grace window does NOT roll back.
#
# Loop termination (Oracle F3): after CANARY_HEALTHY_THRESHOLD consecutive
# healthy checks this script writes a `promote-ready` marker and reports
# `CANARY HEALTHY → promote-ready`. It NEVER auto-promotes — promotion is the
# deliberate `make promote` step. The deploy_canary.sh loop exits once the
# marker appears; `make promote` / `make rollback` stop the loop first.
#
# Transport: Prometheus is a ClusterIP service, reached from the host via a
# short-lived `kubectl port-forward` (kubectl picks a free local port; verified
# ready before use, torn down on exit). Set PROM_BASE_URL to bypass the
# port-forward and point at any Prometheus-compatible query API (used by QA to
# stub a synthetic p95 spike / malformed result).
#
# Docker/kind run ROOTLESS on a user socket -> kubectl needs the kind context
# (defaulted here; the Makefile also exports DOCKER_HOST for kind/docker).
#
# Usage:
#   scripts/canary_check.sh                 # one check (in-cluster Prometheus)
#   PROM_BASE_URL=http://127.0.0.1:9999 scripts/canary_check.sh   # stub API
#   make canary-check
# ============================================================================
set -uo pipefail

# --- configuration ----------------------------------------------------------
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1000/docker-me.sock}"

CLUSTER_NAME="inferenceops"
K8S_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
NAMESPACE="${NAMESPACE:-inferenceops}"

PROM_NAMESPACE="monitoring"
PROM_SVC="kube-prometheus-kube-prome-prometheus"
PROM_SVC_PORT="9090"

CANARY_DEPLOY="model-server-canary"
STABLE_ARTIFACT_META="models/artifacts/fraud_detector/model_meta.json"
CANARY_ARTIFACT="models/artifacts/fraud_detector_canary/model.pkl"

# Thresholds (spec §M8).
P95_RATIO_THRESHOLD="${P95_RATIO_THRESHOLD:-1.20}"     # candidate p95 > stable × 1.20
ERROR_RATE_THRESHOLD="${ERROR_RATE_THRESHOLD:-0.01}"   # candidate error ratio > 1%
EVAL_DROP_THRESHOLD="${EVAL_DROP_THRESHOLD:-0.03}"     # accuracy/f1 drop > 3%

# Automation driver (Oracle S9 / F3).
CANARY_HEALTHY_THRESHOLD="${CANARY_HEALTHY_THRESHOLD:-3}"  # N consecutive healthy
PROM_DOWN_GRACE_SECONDS="${PROM_DOWN_GRACE_SECONDS:-60}"   # unreachable > 60s
QUERY_TIMEOUT="${QUERY_TIMEOUT:-10}"                       # bounded curl (hung_commands)
# Warm-up grace for a freshly-deployed canary: until the canary Deployment is
# this old, a not-yet-measurable p95 is treated as warm-up (Prometheus still
# discovering/scraping the canary ServiceMonitor, traffic still ramping) rather
# than an ambiguity. Bounded so a mature canary with no measurable p95 still
# fails-closed (Oracle S8).
CANARY_WARMUP_GRACE_SECONDS="${CANARY_WARMUP_GRACE_SECONDS:-240}"

# State shared with deploy_canary.sh / promote.sh / rollback.sh.
CANARY_LOOP_PIDFILE="${TMPDIR:-/tmp}/inferenceops-canary-check.pid"
CANARY_READY_MARKER="${TMPDIR:-/tmp}/inferenceops-canary-promote-ready"
CANARY_HEALTHY_COUNT_FILE="${TMPDIR:-/tmp}/inferenceops-canary-healthy-count"
CANARY_DOWN_SINCE_FILE="${TMPDIR:-/tmp}/inferenceops-canary-prom-down-since"

# Optional override: point at a Prometheus-compatible query API (QA stub).
PROM_BASE_URL="${PROM_BASE_URL:-}"

# --- locate repo root (script lives in <root>/scripts) ----------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# --- helpers ----------------------------------------------------------------
log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }
err()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; }

# json_field <file> <key> — read a top-level string field from a JSON object.
json_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

# --- fail-closed / rollback state machine -----------------------------------
# CHECK_DONE marks a DELIBERATE terminal decision (healthy / breach-rollback /
# fail-closed-rollback / within-grace-wait / interrupted). The EXIT trap only
# fires the conservative rollback when we exit WITHOUT such a decision, i.e. an
# unexpected script bug (set -u abort, pipefail, uncaught failure).
CHECK_DONE=0
ROLLBACK_STARTED=0
PF_PID=""
PF_LOG=""
PROM_URL=""

# do_rollback <reason> — the single rollback path for BOTH a threshold breach
# and a fail-closed conservative rollback (Oracle S8: same path as
# Prometheus-unreachable). Runs scripts/rollback.sh (make rollback).
do_rollback() {
  local reason="$1"
  CHECK_DONE=1
  ROLLBACK_STARTED=1
  printf '\n\033[1;31mROLLBACK TRIGGERED: %s\033[0m\n' "$reason"
  # Reset all per-canary check state.
  rm -f "$CANARY_HEALTHY_COUNT_FILE" "$CANARY_READY_MARKER" "$CANARY_DOWN_SINCE_FILE"
  # We may be running inside the check loop's process group. Remove the loop
  # pidfile FIRST so rollback.sh's loop-stop does not SIGTERM our own process
  # group (which would kill this very rollback mid-flight). The loop terminates
  # on its next wake via its pidfile-gone / canary-gone guard.
  rm -f "$CANARY_LOOP_PIDFILE"
  if scripts/rollback.sh; then
    printf '\033[1;32mROLLBACK COMPLETE: canary removed, base ingress restored\033[0m\n'
  else
    err "rollback.sh failed; retrying once (fail-closed demands rollback)"
    if scripts/rollback.sh; then
      printf '\033[1;32mROLLBACK COMPLETE (retry): canary removed, base ingress restored\033[0m\n'
    else
      err "rollback.sh failed twice — canary may remain; run 'make rollback' manually"
    fi
  fi
  exit 1
}

# fail_closed <reason> — unexpected error/ambiguity -> conservative rollback.
fail_closed() {
  err "fail-closed: $1"
  do_rollback "conservative fail-closed rollback: $1"
}

# on_signal — external kill (e.g. `make promote` / `make rollback` stopping the
# loop). A deliberate human action now OWNS the canary state, so we must NOT
# roll back: clean up the port-forward and exit without triggering fail-closed.
# shellcheck disable=SC2329  # invoked indirectly via `trap on_signal INT TERM`
on_signal() {
  CHECK_DONE=1
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null
  printf 'canary_check interrupted — exiting without rollback (promote/rollback owns state)\n'
  exit 130
}

# on_exit — cleanup + fail-closed safety net for uncaught script bugs.
# shellcheck disable=SC2329  # invoked indirectly via `trap on_exit EXIT`
on_exit() {
  local rc=$?
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null
  [[ -n "$PF_LOG" ]] && rm -f "$PF_LOG"
  if [[ "$CHECK_DONE" -ne 1 && "$rc" -ne 0 && "$ROLLBACK_STARTED" -ne 1 ]]; then
    err "unexpected exit (code ${rc}) — fail-closed conservative rollback"
    rm -f "$CANARY_LOOP_PIDFILE" "$CANARY_HEALTHY_COUNT_FILE" \
          "$CANARY_READY_MARKER" "$CANARY_DOWN_SINCE_FILE"
    scripts/rollback.sh || scripts/rollback.sh \
      || err "rollback failed in fail-closed EXIT trap; run 'make rollback' manually"
  fi
}

trap on_signal INT TERM
trap on_exit EXIT

# --- Prometheus transport ---------------------------------------------------
# ensure_prometheus: establish a reachable PROM_URL. Uses PROM_BASE_URL when set
# (QA stub); otherwise a short-lived `kubectl port-forward` on a kubectl-chosen
# free local port (no port conflicts), verified ready before use.
# Returns 0 when reachable, 1 otherwise.
ensure_prometheus() {
  if [[ -n "$PROM_BASE_URL" ]]; then
    PROM_URL="${PROM_BASE_URL%/}"
    if curl -sS --max-time "$QUERY_TIMEOUT" "${PROM_URL}/-/ready" >/dev/null 2>&1; then
      return 0
    fi
    warn "PROM_BASE_URL (${PROM_URL}) not ready"
    return 1
  fi

  PF_LOG="$(mktemp /tmp/inferenceops-canary-pf.XXXXXX)"
  kubectl --context "$K8S_CONTEXT" -n "$PROM_NAMESPACE" \
    port-forward "svc/${PROM_SVC}" ":${PROM_SVC_PORT}" >"$PF_LOG" 2>&1 &
  PF_PID=$!

  local local_port="" _
  for _ in $(seq 1 40); do
    local_port="$(grep -oE '127\.0\.0\.1:[0-9]+' "$PF_LOG" 2>/dev/null | head -1 | cut -d: -f2)"
    [[ -n "$local_port" ]] && break
    # Bail early if the port-forward process already died (e.g. no endpoints).
    kill -0 "$PF_PID" 2>/dev/null || break
    sleep 0.25
  done
  if [[ -z "$local_port" ]]; then
    warn "kubectl port-forward to ${PROM_SVC} did not establish"
    return 1
  fi
  PROM_URL="http://localhost:${local_port}"
  if curl -sS --max-time "$QUERY_TIMEOUT" "${PROM_URL}/-/ready" >/dev/null 2>&1; then
    return 0
  fi
  warn "Prometheus port-forward up at ${PROM_URL} but /-/ready failed"
  return 1
}

# prom_query <promql> — print the raw JSON response; non-zero on transport fail.
prom_query() {
  curl -sS --max-time "$QUERY_TIMEOUT" -G "${PROM_URL}/api/v1/query" \
    --data-urlencode "query=$1"
}

# parse_prom_scalar — read Prometheus JSON on stdin; print the first scalar
# value, or "EMPTY" when the result vector is empty. Exit non-zero on malformed
# JSON or a non-"success" status (so the caller can fail-closed).
parse_prom_scalar() {
  python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(3)
if d.get("status") != "success":
    sys.exit(4)
res = d.get("data", {}).get("result", [])
if not res:
    print("EMPTY")
    sys.exit(0)
v = res[0].get("value")
if not v or len(v) < 2 or v[1] is None:
    print("EMPTY")
    sys.exit(0)
print(v[1])
'
}

# query_scalar <promql> — print scalar/EMPTY; non-zero on transport or malformed.
query_scalar() {
  local raw
  raw="$(prom_query "$1")" || return 1
  printf '%s' "$raw" | parse_prom_scalar || return 1
}

# is_finite_number <s> — true for a finite, non-negative numeric string
# (rejects "", "EMPTY", "NaN", "+Inf", "-Inf").
is_finite_number() {
  [[ "$1" =~ ^[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?$ ]]
}

# canary_younger_than <seconds> — true if the canary Deployment's age is below
# <seconds> (used to bound the warm-up grace). Returns non-zero when the age
# cannot be determined, so an unknown age is NOT treated as young (fail-closed).
canary_younger_than() {
  local grace="$1" created created_epoch now_epoch
  created="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get deployment "$CANARY_DEPLOY" \
    -o jsonpath='{.metadata.creationTimestamp}' 2>/dev/null || true)"
  [[ -n "$created" ]] || return 1
  created_epoch="$(date -d "$created" +%s 2>/dev/null || echo 0)"
  now_epoch="$(date +%s)"
  [[ $(( now_epoch - created_epoch )) -lt "$grace" ]]
}

# --- Prometheus-down handling (grace window across iterations) --------------
handle_prom_unreachable() {
  local now since elapsed
  now="$(date +%s)"
  if [[ -f "$CANARY_DOWN_SINCE_FILE" ]]; then
    since="$(cat "$CANARY_DOWN_SINCE_FILE" 2>/dev/null || echo "$now")"
    [[ "$since" =~ ^[0-9]+$ ]] || since="$now"
    elapsed=$(( now - since ))
    warn "Prometheus unreachable for ${elapsed}s (grace ${PROM_DOWN_GRACE_SECONDS}s)"
    if [[ "$elapsed" -gt "$PROM_DOWN_GRACE_SECONDS" ]]; then
      do_rollback "Prometheus unreachable > ${PROM_DOWN_GRACE_SECONDS}s (conservative fail-closed)"
    fi
  else
    warn "Prometheus unreachable — starting ${PROM_DOWN_GRACE_SECONDS}s grace window"
    echo "$now" > "$CANARY_DOWN_SINCE_FILE"
  fi
  CHECK_DONE=1   # deliberate wait (within grace) — not a fail-closed abort
  exit 0
}

# --- preflight: resolve candidate/stable model_version ----------------------
command -v kubectl >/dev/null 2>&1 || { err "kubectl not found on PATH"; exit 1; }
command -v curl    >/dev/null 2>&1 || { err "curl not found on PATH"; exit 1; }

# Candidate version: read live from the canary Deployment env (deploy_canary.sh
# pins MODEL_VERSION there); fall back to the candidate artifact metadata.
CANDIDATE_VERSION="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" \
  get deployment "$CANARY_DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="MODEL_VERSION")].value}' \
  2>/dev/null || true)"
if [[ -z "$CANDIDATE_VERSION" && -f "models/artifacts/fraud_detector_canary/model_meta.json" ]]; then
  CANDIDATE_VERSION="$(json_field models/artifacts/fraud_detector_canary/model_meta.json model_version 2>/dev/null || true)"
fi
if [[ -z "$CANDIDATE_VERSION" ]]; then
  # No canary deployed -> nothing to protect. Not a fail-closed condition; the
  # deploy_canary.sh loop breaks on its canary-gone guard.
  log "no canary deployment (${CANARY_DEPLOY}) in ${NAMESPACE} — nothing to check"
  CHECK_DONE=1
  exit 0
fi

# Stable version: the stable pods load models/artifacts/fraud_detector, so the
# label they emit is that artifact's model_version (no MODEL_VERSION env on the
# stable Deployment). Fall back to 1.0.0.
STABLE_VERSION=""
if [[ -f "$STABLE_ARTIFACT_META" ]]; then
  STABLE_VERSION="$(json_field "$STABLE_ARTIFACT_META" model_version 2>/dev/null || true)"
fi
STABLE_VERSION="${STABLE_VERSION:-1.0.0}"

log "canary health check: candidate=${CANDIDATE_VERSION} stable=${STABLE_VERSION} (${NAMESPACE} @ ${K8S_CONTEXT})"

# --- establish Prometheus reachability --------------------------------------
if ! ensure_prometheus; then
  handle_prom_unreachable   # exits (rollback if > grace, else wait)
fi
# Reachable: clear any prior down-since tracker (stale_state guard).
rm -f "$CANARY_DOWN_SINCE_FILE"

# --- check 1: p95 latency per version (model_version-filtered) --------------
# The quantile is always 0.95; the +20% lives in the comparison against stable.
CAND_P95_Q="histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket{model_version=\"${CANDIDATE_VERSION}\"}[1m])) by (le))"
STABLE_P95_Q="histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket{model_version=\"${STABLE_VERSION}\"}[1m])) by (le))"

CAND_P95="$(query_scalar "$CAND_P95_Q")" \
  || fail_closed "candidate p95 query failed/malformed (Prometheus reachable)"
if ! is_finite_number "$CAND_P95"; then
  # Candidate p95 is empty/NaN. For a freshly-deployed canary this is expected
  # warm-up (Prometheus still scraping the canary ServiceMonitor / traffic still
  # ramping), so WAIT rather than roll back — but only within the bounded grace;
  # a mature canary with no measurable p95 is genuinely ambiguous -> fail-closed.
  if canary_younger_than "$CANARY_WARMUP_GRACE_SECONDS"; then
    log "CANARY WARMING UP: candidate p95 not yet measurable (canary age < ${CANARY_WARMUP_GRACE_SECONDS}s) — waiting, not counted, not rolled back"
    CHECK_DONE=1
    exit 0
  fi
  fail_closed "candidate p95 not measurable (result '${CAND_P95}') — ambiguous"
fi

STABLE_P95="$(query_scalar "$STABLE_P95_Q")" \
  || fail_closed "stable p95 query failed/malformed (Prometheus reachable)"
is_finite_number "$STABLE_P95" \
  || fail_closed "stable p95 not measurable (result '${STABLE_P95}') — ambiguous"

printf 'p95 latency: candidate=%ss stable=%ss (rollback if candidate > stable × %s)\n' \
  "$CAND_P95" "$STABLE_P95" "$P95_RATIO_THRESHOLD"
if awk -v c="$CAND_P95" -v s="$STABLE_P95" -v r="$P95_RATIO_THRESHOLD" \
     'BEGIN{exit !(c > s * r)}'; then
  do_rollback "p95 regression: candidate ${CAND_P95}s > stable ${STABLE_P95}s × ${P95_RATIO_THRESHOLD}"
fi
printf '  -> OK\n'

# --- check 2: error rate per version (model_version-filtered) ---------------
# Error RATIO = error_count rate / request_count rate (the spec's ">1%" is a
# percentage). Counters are stored with the `_total` suffix by prometheus_client.
# An empty numerator (no error series yet) means zero errors -> ratio 0.
ERR_Q="sum(rate(inferenceops_error_count_total{model_version=\"${CANDIDATE_VERSION}\"}[1m])) / sum(rate(inferenceops_request_count_total{model_version=\"${CANDIDATE_VERSION}\"}[1m]))"
ERR_RATIO="$(query_scalar "$ERR_Q")" \
  || fail_closed "error-rate query failed/malformed (Prometheus reachable)"
# No error series (EMPTY) or 0/0 (NaN) => no errors observed => 0.
is_finite_number "$ERR_RATIO" || ERR_RATIO="0"
printf 'error rate: candidate=%s (rollback if > %s)\n' "$ERR_RATIO" "$ERROR_RATE_THRESHOLD"
if awk -v e="$ERR_RATIO" -v t="$ERROR_RATE_THRESHOLD" 'BEGIN{exit !(e > t)}'; then
  do_rollback "error-rate regression: candidate error ratio ${ERR_RATIO} > ${ERROR_RATE_THRESHOLD}"
fi
printf '  -> OK\n'

# --- check 3: eval score (candidate artifact vs baseline) -------------------
# Score the candidate artifact on the golden dataset and compare against
# evals/reports/baseline.json. Reuses the evals/ machinery without modifying it.
EVAL_OUT="$(uv run python - <<'PY' 2>/dev/null
import sys
sys.path.insert(0, "evals")
from pathlib import Path
from eval_common import BASELINE_PATH, load_golden_dataset, read_metrics
from sklearn_seam import accuracy, f1, load_model, predict_labels

cand = Path("models/artifacts/fraud_detector_canary/model.pkl")
if not cand.exists():
    print("MISSING")
    sys.exit(0)
model = load_model(cand)
golden = load_golden_dataset()
y_pred = predict_labels(model, golden.features)
acc = accuracy(golden.labels, y_pred)
f1_score = f1(golden.labels, y_pred)
base = read_metrics(BASELINE_PATH)
print(f"{base.accuracy - acc:.6f} {base.f1 - f1_score:.6f} "
      f"{acc:.4f} {f1_score:.4f} {base.accuracy:.4f} {base.f1:.4f}")
PY
)" || fail_closed "eval check failed to run (uv/python error)"

if [[ "$EVAL_OUT" == "MISSING" || -z "$EVAL_OUT" ]]; then
  fail_closed "candidate artifact missing/unreadable for eval (${CANARY_ARTIFACT}) — ambiguous"
fi
DROP_ACC="$(awk '{print $1}' <<< "$EVAL_OUT")"
DROP_F1="$(awk '{print $2}' <<< "$EVAL_OUT")"
CAND_ACC="$(awk '{print $3}' <<< "$EVAL_OUT")"
CAND_F1="$(awk '{print $4}' <<< "$EVAL_OUT")"
BASE_ACC="$(awk '{print $5}' <<< "$EVAL_OUT")"
BASE_F1="$(awk '{print $6}' <<< "$EVAL_OUT")"
is_finite_number "${DROP_ACC#-}" || fail_closed "eval result malformed: '${EVAL_OUT}'"
printf 'eval score: candidate acc=%s f1=%s (baseline acc=%s f1=%s); drop acc=%s f1=%s (rollback if drop > %s)\n' \
  "$CAND_ACC" "$CAND_F1" "$BASE_ACC" "$BASE_F1" "$DROP_ACC" "$DROP_F1" "$EVAL_DROP_THRESHOLD"
# A negative drop means the candidate is BETTER than baseline -> never a breach.
if awk -v da="$DROP_ACC" -v df="$DROP_F1" -v t="$EVAL_DROP_THRESHOLD" \
     'BEGIN{exit !(da > t || df > t)}'; then
  do_rollback "eval regression: accuracy/f1 drop > ${EVAL_DROP_THRESHOLD} (drop_acc=${DROP_ACC}, drop_f1=${DROP_F1})"
fi
printf '  -> OK\n'

# --- check 4: GPU mem (DCGM) — INFORMATIONAL only ---------------------------
GPU_USED="$(query_scalar 'DCGM_FI_DEV_FB_USED' 2>/dev/null || true)"
if is_finite_number "$GPU_USED"; then
  printf 'GPU mem (DCGM_FI_DEV_FB_USED): %s MiB (informational; no GPU threshold enforced)\n' "$GPU_USED"
else
  printf 'GPU mem: no DCGM metrics present (no GPU) — skipped\n'
fi

# --- all checks passed: record healthy + maybe promote-ready ----------------
HEALTHY_COUNT=0
if [[ -f "$CANARY_HEALTHY_COUNT_FILE" ]]; then
  HEALTHY_COUNT="$(cat "$CANARY_HEALTHY_COUNT_FILE" 2>/dev/null || echo 0)"
  [[ "$HEALTHY_COUNT" =~ ^[0-9]+$ ]] || HEALTHY_COUNT=0
fi
HEALTHY_COUNT=$(( HEALTHY_COUNT + 1 ))
echo "$HEALTHY_COUNT" > "$CANARY_HEALTHY_COUNT_FILE"

if [[ "$HEALTHY_COUNT" -ge "$CANARY_HEALTHY_THRESHOLD" ]]; then
  : > "$CANARY_READY_MARKER"   # promote-ready flag (NO auto-promote)
  printf '\n\033[1;32mCANARY HEALTHY → promote-ready\033[0m (%s/%s consecutive healthy checks)\n' \
    "$HEALTHY_COUNT" "$CANARY_HEALTHY_THRESHOLD"
  printf 'Promotion is deliberate — run: make promote\n'
else
  printf '\n\033[1;32mCANARY HEALTHY\033[0m (%s/%s consecutive healthy checks)\n' \
    "$HEALTHY_COUNT" "$CANARY_HEALTHY_THRESHOLD"
fi

CHECK_DONE=1
exit 0
