#!/usr/bin/env bash
# ============================================================================
# rollback.sh — roll the canary back to 0% candidate and restore the base
# ingress (todo 29). Wired to `make rollback`.
#
# Steps (idempotent / safe to re-run from any state — canary @10%, promoted
# @100%, or partially torn down):
#   1. STOP the automated canary check loop (todo 30 convention) so it cannot
#      fire against a half-removed canary. No-op when none is running.
#   2. delete the canary Ingress, Deployment and Service (--ignore-not-found).
#   3. RESTORE the base ingress (k8s/base/ingress.yaml): the `inferenceops`
#      Ingress points back at service-gateway, reinstating auth/rate-limit and
#      the base ingress -> gateway -> model-server path.
#   4. verify: base ingress -> service-gateway, canary objects gone (0% candidate).
#
# The stable model-server Deployment is never touched — it has been serving 1.0.0
# the whole time; rollback just removes the canary half and the gateway bypass.
#
# Usage:  scripts/rollback.sh   |   make rollback
# ============================================================================
set -euo pipefail

K8S_CONTEXT="${KUBE_CONTEXT:-kind-inferenceops}"
NAMESPACE="${NAMESPACE:-inferenceops}"
CANARY_INGRESS="inferenceops-canary"
CANARY_DEPLOY="model-server-canary"
CANARY_SVC="service-model-server-canary"
BASE_INGRESS_MANIFEST="k8s/base/ingress.yaml"

# Shared with deploy_canary.sh / canary_check.sh (todo 30).
CANARY_LOOP_PIDFILE="${TMPDIR:-/tmp}/inferenceops-canary-check.pid"
CANARY_READY_MARKER="${TMPDIR:-/tmp}/inferenceops-canary-promote-ready"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. stop the automated check loop (todo 30) -----------------------------
if [[ -f "$CANARY_LOOP_PIDFILE" ]]; then
  LOOP_PID="$(cat "$CANARY_LOOP_PIDFILE" 2>/dev/null || true)"
  if [[ -n "$LOOP_PID" ]] && kill -0 "$LOOP_PID" 2>/dev/null; then
    log "stopping canary check loop (pid ${LOOP_PID})"
    kill "$LOOP_PID" 2>/dev/null || true
    kill -- "-${LOOP_PID}" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$LOOP_PID" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$LOOP_PID" 2>/dev/null && kill -9 "$LOOP_PID" 2>/dev/null || true
  fi
  rm -f "$CANARY_LOOP_PIDFILE"
else
  log "no canary check loop running — nothing to stop"
fi
rm -f "$CANARY_READY_MARKER"

# --- 2. delete the canary objects (0% candidate) ----------------------------
log "deleting canary Ingress / Deployment / Service (ignore-not-found)"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" delete ingress "$CANARY_INGRESS" --ignore-not-found
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" delete deployment "$CANARY_DEPLOY" --ignore-not-found
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" delete service "$CANARY_SVC" --ignore-not-found

# --- 3. restore the base ingress (-> service-gateway) -----------------------
# Same object name (`inferenceops`) as the canary stable-ingress, so apply
# overwrites it back to the base backend (service-gateway). Auth/rate-limit
# (the gateway) are reinstated.
[[ -f "$BASE_INGRESS_MANIFEST" ]] || die "base ingress manifest missing: ${BASE_INGRESS_MANIFEST}"
log "restoring base ingress (${BASE_INGRESS_MANIFEST} -> service-gateway)"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" apply -f "$BASE_INGRESS_MANIFEST"

# --- 4. verify: base restored, canary gone ----------------------------------
BACKEND="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get ingress inferenceops \
  -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}')"
[[ "$BACKEND" == "service-gateway" ]] \
  || die "base ingress backend is '${BACKEND}', expected service-gateway (restore failed)"
log "verified: ingress inferenceops -> ${BACKEND} (base path restored)"

if kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get ingress "$CANARY_INGRESS" >/dev/null 2>&1; then
  die "canary Ingress ${CANARY_INGRESS} still present after rollback"
fi
if kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get deployment "$CANARY_DEPLOY" >/dev/null 2>&1; then
  die "canary Deployment ${CANARY_DEPLOY} still present after rollback"
fi
log "verified: canary Ingress + Deployment removed (0% candidate, no orphan)"

cat <<EOF

Rolled back: 0% candidate. Base ingress restored.
  ingress inferenceops -> service-gateway -> service-model-server (model_version=1.0.0)
  auth/rate-limit (gateway) reinstated; /v1/predict needs X-API-Key again.
EOF
