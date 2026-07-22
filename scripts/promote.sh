#!/usr/bin/env bash
# ============================================================================
# promote.sh — promote the canary to 100% candidate (todo 29). Wired to
# `make promote` (the deliberate gated step; the automated check never
# auto-promotes — todo 30 only reports promote-ready).
#
# Steps (idempotent / safe to re-run):
#   1. STOP the automated canary check loop FIRST (todo 30 convention): kill the
#      loop PID and clear its promote-ready marker so a concurrent / post-promote
#      check cannot race the promotion or roll back the just-promoted version
#      (Oracle F3/r3). No-op when no loop is running (todo 29 starts none).
#   2. raise the canary Ingress weight to 100 -> 100% of host inferenceops.local
#      goes to service-model-server-canary (candidate).
#   3. verify the weight and that the candidate is actually serving.
#
# The gateway stays bypassed while the canary pair owns the host; `make rollback`
# reinstates the base ingress (-> gateway, auth/rate-limit restored).
#
# Usage:  scripts/promote.sh   |   make promote
# ============================================================================
set -euo pipefail

K8S_CONTEXT="${KUBE_CONTEXT:-kind-inferenceops}"
NAMESPACE="${NAMESPACE:-inferenceops}"
CANARY_INGRESS="inferenceops-canary"
CANARY_DEPLOY="model-server-canary"

# Shared with deploy_canary.sh / canary_check.sh (todo 30).
CANARY_LOOP_PIDFILE="${TMPDIR:-/tmp}/inferenceops-canary-check.pid"
CANARY_READY_MARKER="${TMPDIR:-/tmp}/inferenceops-canary-promote-ready"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0. preflight: a canary must be deployed to promote ---------------------
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get ingress "$CANARY_INGRESS" >/dev/null 2>&1 \
  || die "no canary Ingress (${CANARY_INGRESS}) in ${NAMESPACE} — run 'make deploy-canary MODEL_VERSION=<v>' first"

# --- 1. stop the automated check loop (todo 30) before shifting weight ------
# Kill the loop and give any in-flight canary_check.sh iteration a moment to
# finish, then clear the marker — so the weight shift cannot race a check, and
# the degenerate post-promote case (stable traffic -> 0 -> empty candidate query
# -> fail-closed rollback) cannot fire against a just-promoted version.
if [[ -f "$CANARY_LOOP_PIDFILE" ]]; then
  LOOP_PID="$(cat "$CANARY_LOOP_PIDFILE" 2>/dev/null || true)"
  if [[ -n "$LOOP_PID" ]] && kill -0 "$LOOP_PID" 2>/dev/null; then
    log "stopping canary check loop (pid ${LOOP_PID})"
    kill "$LOOP_PID" 2>/dev/null || true
    # Best-effort: also signal the loop's process group (the while-subshell +
    # any running canary_check.sh child). Ignore errors (group may not exist).
    kill -- "-${LOOP_PID}" 2>/dev/null || true
    # Bound the wait so promote never hangs on a stuck iteration.
    for _ in 1 2 3 4 5; do
      kill -0 "$LOOP_PID" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$LOOP_PID" 2>/dev/null && { log "WARN: loop ${LOOP_PID} still alive; sending SIGKILL"; kill -9 "$LOOP_PID" 2>/dev/null || true; }
  else
    log "canary check loop pidfile present but process not running (stale); clearing"
  fi
  rm -f "$CANARY_LOOP_PIDFILE"
else
  log "no canary check loop running (todo 29 starts none) — nothing to stop"
fi
rm -f "$CANARY_READY_MARKER"

# --- 2. shift the canary weight to 100 (100% candidate) ---------------------
log "setting ${CANARY_INGRESS} canary-weight=100 (100% candidate)"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" annotate ingress "$CANARY_INGRESS" \
  nginx.ingress.kubernetes.io/canary-weight="100" --overwrite

# --- 3. verify --------------------------------------------------------------
WEIGHT="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get ingress "$CANARY_INGRESS" \
  -o jsonpath='{.metadata.annotations.nginx\.ingress\.kubernetes\.io/canary-weight}')"
[[ "$WEIGHT" == "100" ]] || die "canary-weight is '${WEIGHT}', expected 100"
log "verified: ${CANARY_INGRESS} canary-weight=${WEIGHT}"

# Confirm the candidate pods are Ready (so 100% candidate is actually servable).
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" wait \
  --for=condition=Ready pod -l "app.kubernetes.io/name=${CANARY_DEPLOY}" --timeout=60s \
  || die "canary pods not Ready — promotion would black-hole traffic"

# --- 4. wait for the nginx data plane to reconcile (best-effort) ------------
# The annotation is the k8s source of truth, but the ingress controller takes a
# few seconds to reload its config; traffic sent in that window can still hit the
# stable backend. Probe live traffic (bounded) until the candidate version is
# actually being served so `make promote` returns only once 100% candidate is in
# effect. Best-effort: an unreachable endpoint (e.g. non-local context) or a
# timeout does NOT fail the promotion — the weight is already set and verified.
INGRESS_HOST="inferenceops.local"
INGRESS_PORT="${INGRESS_PORT:-8080}"
CANDIDATE_VERSION="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get deployment "$CANARY_DEPLOY" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="MODEL_VERSION")].value}' 2>/dev/null || true)"
# Demo fraud-detector predict shape (20 finite features); probe payload only.
PROBE_BODY='{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'
if [[ -n "$CANDIDATE_VERSION" ]]; then
  log "waiting (<=30s) for nginx to serve 100% candidate (model_version=${CANDIDATE_VERSION})"
  RECONCILED=0
  for _ in $(seq 1 15); do
    RESP="$(curl -s --max-time 3 -X POST "http://localhost:${INGRESS_PORT}/v1/predict" \
      -H "Host: ${INGRESS_HOST}" -H 'Content-Type: application/json' -d "$PROBE_BODY" 2>/dev/null || true)"
    if [[ "$RESP" == *"\"model_version\":\"${CANDIDATE_VERSION}\""* ]]; then
      RECONCILED=1
      break
    fi
    sleep 2
  done
  if [[ "$RECONCILED" -eq 1 ]]; then
    log "verified: live traffic serving candidate model_version=${CANDIDATE_VERSION} (100%)"
  else
    log "WARN: could not confirm live candidate traffic within 30s (endpoint unreachable or still reconciling); canary-weight=100 is set"
  fi
else
  log "note: could not read candidate MODEL_VERSION; skipping live-traffic reconcile probe"
fi

cat <<EOF

Promoted: 100% of host inferenceops.local -> ${CANARY_DEPLOY} (candidate).
  canary-weight=${WEIGHT} on ${CANARY_INGRESS}; automated check loop stopped.
  The gateway is still bypassed (canary pair owns the host).

  Roll back to stable + base ingress:  make rollback
EOF
