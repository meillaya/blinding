#!/usr/bin/env bash
# ============================================================================
# install_prometheus.sh — kube-prometheus-stack via Helm (todo 25).
#
# Installs (or upgrades) the kube-prometheus-stack Helm release into the
# `monitoring` namespace — the in-cluster Prometheus the custom-RPS HPA
# (todo 20), canary rollback (todo 30) and dashboards/alerts (todo 32) depend
# on. This is the standalone, any-cluster counterpart of the Prometheus step
# baked into scripts/deploy_local.sh (todo 19).
#
# IDEMPOTENT: uses `helm upgrade --install` (install if absent, upgrade if
# present) with `--create-namespace`, so a re-run converges to the same
# healthy state. If observability/prometheus/values.yaml exists it is used;
# otherwise the chart's defaults are installed (custom values/alerts/
# dashboards arrive in todo 32).
#
# The kube context is taken from the first argument, else $KUBE_CONTEXT, else
# the local kind cluster context `kind-inferenceops`.
#
# Usage:
#   infra/scripts/install_prometheus.sh
#   infra/scripts/install_prometheus.sh kind-inferenceops
#   KUBE_CONTEXT=staging infra/scripts/install_prometheus.sh
#
# Requires: kubectl + helm (configured for the target context).
# ============================================================================
set -euo pipefail

# --- configuration ----------------------------------------------------------
KUBE_CONTEXT="${1:-${KUBE_CONTEXT:-kind-inferenceops}}"

HELM_RELEASE="kube-prometheus"
HELM_CHART="prometheus-community/kube-prometheus-stack"
HELM_REPO_NAME="prometheus-community"
HELM_REPO_URL="https://prometheus-community.github.io/helm-charts"
MONITORING_NS="monitoring"
VALUES_FILE="observability/prometheus/values.yaml"

# --- locate repo root (script lives in <root>/infra/scripts) ----------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# wait_pods_ready <namespace> <label-selector> <timeout-seconds>
# Polls until every pod matching the selector exists AND is Ready, or times out.
# `kubectl wait` errors when no pods match yet, so we retry until the deadline.
wait_pods_ready() {
  local ns="$1" selector="$2" timeout="$3"
  log "waiting (<=${timeout}s) for Ready pods: -n ${ns} -l ${selector}"
  local deadline=$(( $(date +%s) + timeout ))
  while true; do
    if kubectl --context "$KUBE_CONTEXT" -n "$ns" wait \
        --for=condition=Ready pod -l "$selector" --timeout=5s >/dev/null 2>&1; then
      log "ready: -n ${ns} -l ${selector}"
      return 0
    fi
    if (( $(date +%s) > deadline )); then
      log "TIMEOUT waiting for: -n ${ns} -l ${selector}; current pods:"
      kubectl --context "$KUBE_CONTEXT" -n "$ns" get pods -l "$selector" || true
      return 1
    fi
    sleep 5
  done
}

# --- 0. sanity --------------------------------------------------------------
log "prometheus context: $KUBE_CONTEXT"
for bin in kubectl helm; do
  command -v "$bin" >/dev/null 2>&1 || die "required binary not found on PATH: $bin"
done
kubectl config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 \
  || die "kube context not found: $KUBE_CONTEXT (is the cluster up?)"

# --- 1. helm repo -----------------------------------------------------------
log "adding helm repo: $HELM_REPO_NAME"
helm repo add "$HELM_REPO_NAME" "$HELM_REPO_URL" >/dev/null 2>&1 || true
helm repo update "$HELM_REPO_NAME" >/dev/null

# --- 2. install / upgrade ---------------------------------------------------
VALUES_ARGS=()
if [[ -f "$VALUES_FILE" ]]; then
  log "using values file: $VALUES_FILE"
  VALUES_ARGS=(--values "$VALUES_FILE")
else
  log "no $VALUES_FILE — installing with chart defaults (todo 32 adds values/alerts/dashboards)"
fi

# `helm status` tells us whether the release already exists (idempotent report;
# upgrade --install is itself idempotent regardless of the outcome here).
if helm --kube-context "$KUBE_CONTEXT" status "$HELM_RELEASE" -n "$MONITORING_NS" >/dev/null 2>&1; then
  log "release ${HELM_RELEASE} exists in ns ${MONITORING_NS} — upgrading"
else
  log "release ${HELM_RELEASE} not found in ns ${MONITORING_NS} — installing"
fi

log "helm upgrade --install ${HELM_RELEASE} ${HELM_CHART} (ns: ${MONITORING_NS})"
# No --wait: we poll the Prometheus pod ourselves with a bounded wait (helm
# --wait on this chart can trip over non-critical test/hook resources).
helm --kube-context "$KUBE_CONTEXT" upgrade --install "$HELM_RELEASE" "$HELM_CHART" \
  -n "$MONITORING_NS" --create-namespace \
  --timeout 10m \
  "${VALUES_ARGS[@]}"

# --- 3. wait for readiness --------------------------------------------------
# The prometheus-operator must reconcile the Prometheus CR before the server
# pod exists; the poll loop tolerates that "no pods yet" window. The selector
# matches ONLY the server pod (operator/node-exporter carry different labels).
wait_pods_ready "$MONITORING_NS" "app.kubernetes.io/name=prometheus" 420
# Grafana ships with the stack; wait for it too (best evidence the stack is up),
# but do not fail the install if it lags (Prometheus is the real dependency).
wait_pods_ready "$MONITORING_NS" "app.kubernetes.io/name=grafana" 300 || \
  log "WARN: grafana not Ready yet (non-fatal; Prometheus is the dependency)"

# --- summary ----------------------------------------------------------------
log "kube-prometheus-stack reconciled on context $KUBE_CONTEXT"
helm --kube-context "$KUBE_CONTEXT" status "$HELM_RELEASE" -n "$MONITORING_NS"
kubectl --context "$KUBE_CONTEXT" -n "$MONITORING_NS" get pods
