#!/usr/bin/env bash
# ============================================================================
# bootstrap_cluster.sh — post-cluster bootstrap (todo 25).
#
# Installs the cluster-level addons the platform depends on, in order:
#   1. metrics-server   -> resource metrics (HPA cpu/memory, `kubectl top`)
#   2. ingress-nginx    -> the public edge (fronts the gateway Ingress)
#   3. cert-manager     -> TLS issuance for the Ingress (staging/prod)
#
# IDEMPOTENT: each component is checked before install (a re-run skips what is
# already present and converges to the same state), and every install is
# followed by a bounded readiness wait so the script only returns once the
# component is actually serving.
#
# The kube context is taken from the first argument, else $KUBE_CONTEXT, else
# the local kind cluster context `kind-inferenceops`.
#
# Usage:
#   infra/scripts/bootstrap_cluster.sh                      # kind-inferenceops
#   infra/scripts/bootstrap_cluster.sh kind-inferenceops
#   KUBE_CONTEXT=staging infra/scripts/bootstrap_cluster.sh
#
# Requires: kubectl (configured for the target context).
# ============================================================================
set -euo pipefail

# --- configuration ----------------------------------------------------------
KUBE_CONTEXT="${1:-${KUBE_CONTEXT:-kind-inferenceops}}"

METRICS_SERVER_URL="https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
# Match the ingress-nginx version deploy_local.sh pins (todo 19) so the local
# kind stack and a bootstrapped cluster run the same controller.
INGRESS_NGINX_VERSION="controller-v1.15.1"
INGRESS_NGINX_KIND_URL="https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/kind/deploy.yaml"
INGRESS_NGINX_CLOUD_URL="https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/cloud/deploy.yaml"
CERT_MANAGER_VERSION="v1.17.1"
CERT_MANAGER_URL="https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"

# --- locate repo root (script lives in <root>/infra/scripts) ----------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# wait_rollout <namespace> <resource> <timeout-seconds>
wait_rollout() {
  local ns="$1" resource="$2" timeout="$3"
  log "waiting (<=${timeout}s) for rollout: -n ${ns} ${resource}"
  kubectl --context "$KUBE_CONTEXT" -n "$ns" rollout status "$resource" --timeout="${timeout}s"
}

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
log "bootstrap context: $KUBE_CONTEXT"
command -v kubectl >/dev/null 2>&1 || die "required binary not found on PATH: kubectl"
kubectl config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 \
  || die "kube context not found: $KUBE_CONTEXT (is the cluster up? try scripts/deploy_local.sh)"
kubectl --context "$KUBE_CONTEXT" wait --for=condition=Ready node --all --timeout=120s >/dev/null 2>&1 \
  || die "no Ready node on context $KUBE_CONTEXT"

# --- 1. metrics-server ------------------------------------------------------
if kubectl --context "$KUBE_CONTEXT" -n kube-system get deployment metrics-server >/dev/null 2>&1; then
  log "metrics-server: already installed (skipping apply)"
else
  log "metrics-server: installing from ${METRICS_SERVER_URL}"
  kubectl --context "$KUBE_CONTEXT" apply -f "$METRICS_SERVER_URL"
fi
# kind kubelets serve self-signed certs, so the stock manifest's TLS check
# fails; add --kubelet-insecure-tls (idempotent: skipped if already present).
if [[ "$KUBE_CONTEXT" == kind-* ]]; then
  ms_args="$(kubectl --context "$KUBE_CONTEXT" -n kube-system get deployment metrics-server \
    -o jsonpath='{.spec.template.spec.containers[0].args[*]}' 2>/dev/null || true)"
  if [[ "$ms_args" == *"--kubelet-insecure-tls"* ]]; then
    log "metrics-server: --kubelet-insecure-tls already present (skipping patch)"
  elif [[ -n "$ms_args" ]]; then
    log "metrics-server: kind detected — adding --kubelet-insecure-tls"
    kubectl --context "$KUBE_CONTEXT" -n kube-system patch deployment metrics-server --type=json \
      -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
  else
    log "metrics-server: kind detected — setting args=[--kubelet-insecure-tls]"
    kubectl --context "$KUBE_CONTEXT" -n kube-system patch deployment metrics-server --type=json \
      -p '[{"op":"add","path":"/spec/template/spec/containers/0/args","value":["--kubelet-insecure-tls"]}]'
  fi
fi
wait_rollout "kube-system" "deployment/metrics-server" 300

# --- 2. ingress-nginx -------------------------------------------------------
if kubectl --context "$KUBE_CONTEXT" -n ingress-nginx get deployment ingress-nginx-controller >/dev/null 2>&1; then
  log "ingress-nginx: already installed (skipping apply)"
else
  if [[ "$KUBE_CONTEXT" == kind-* ]]; then
    INGRESS_NGINX_URL="$INGRESS_NGINX_KIND_URL"
  else
    INGRESS_NGINX_URL="$INGRESS_NGINX_CLOUD_URL"
  fi
  log "ingress-nginx: installing ${INGRESS_NGINX_VERSION} from ${INGRESS_NGINX_URL}"
  kubectl --context "$KUBE_CONTEXT" apply -f "$INGRESS_NGINX_URL"
fi
wait_pods_ready "ingress-nginx" "app.kubernetes.io/component=controller" 300

# --- 3. cert-manager --------------------------------------------------------
if kubectl --context "$KUBE_CONTEXT" -n cert-manager get deployment cert-manager >/dev/null 2>&1; then
  log "cert-manager: already installed (skipping apply)"
else
  log "cert-manager: installing ${CERT_MANAGER_VERSION} from ${CERT_MANAGER_URL}"
  kubectl --context "$KUBE_CONTEXT" apply -f "$CERT_MANAGER_URL"
fi
# All three cert-manager components must be Ready before it can issue certs.
wait_rollout "cert-manager" "deployment/cert-manager" 300
wait_rollout "cert-manager" "deployment/cert-manager-webhook" 300
wait_rollout "cert-manager" "deployment/cert-manager-cainjector" 300

# --- summary ----------------------------------------------------------------
log "bootstrap complete on context $KUBE_CONTEXT"
kubectl --context "$KUBE_CONTEXT" -n kube-system get deployment metrics-server
kubectl --context "$KUBE_CONTEXT" -n ingress-nginx get deployment ingress-nginx-controller
kubectl --context "$KUBE_CONTEXT" -n cert-manager get deployment \
  cert-manager cert-manager-webhook cert-manager-cainjector
