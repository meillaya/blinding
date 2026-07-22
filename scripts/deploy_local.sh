#!/usr/bin/env bash
# deploy_local.sh — idempotent local full-stack deploy to kind (todo 19).
#
# Brings up, in order:
#   1. the two app images (built only if missing),
#   2. a kind cluster `inferenceops` (skipped if it already exists),
#   3. the nginx ingress controller (the public edge; fronts the gateway),
#   4. kube-prometheus-stack — the in-cluster Prometheus that the custom-RPS
#      HPA (todo 20) and canary rollback (todo 30) depend on,
#   5. `kind load` of both images onto the node (there is NO registry),
#   6. the gateway API-key Secret (the base ships only an example placeholder),
#   7. the inferenceops overlay (model-server + gateway).
#
# Idempotent / re-runnable: cluster create is skipped when the cluster exists,
# helm uses `upgrade --install`, and every kubectl write is a declarative
# `apply`. A re-run converges to the same healthy state.
#
# Docker runs ROOTLESS on a user socket, so every docker/kind call needs
# DOCKER_HOST. We default it here (the Makefile also exports it); override by
# exporting DOCKER_HOST before invoking.
set -euo pipefail

# --- configuration ----------------------------------------------------------
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1000/docker-me.sock}"

CLUSTER_NAME="inferenceops"
K8S_CONTEXT="kind-${CLUSTER_NAME}"
KIND_CONFIG="infra/scripts/kind-config.yaml"
NAMESPACE="inferenceops"

MODEL_IMAGE="inferenceops/model-server:dev"
GATEWAY_IMAGE="inferenceops/gateway:dev"
GATEWAY_DOCKERFILE="apps/gateway/Dockerfile"

INGRESS_MANIFEST_URL="https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/kind/deploy.yaml"

PROM_HELM_RELEASE="kube-prometheus"
PROM_HELM_CHART="prometheus-community/kube-prometheus-stack"
PROM_HELM_REPO_NAME="prometheus-community"
PROM_HELM_REPO_URL="https://prometheus-community.github.io/helm-charts"
PROM_NS="monitoring"
# Detailed values/alerts/dashboards arrive in todo 32 (W7). If a values file is
# present we use it; otherwise we install with the chart's default values.
PROM_VALUES_FILE="observability/prometheus/values.yaml"

# The base kustomization applies k8s/base/secret.example.yaml (dev placeholder
# keys). To make the local QA key authoritative WITHOUT editing the todo-16
# manifests, we overwrite that Secret with this key after applying the overlay
# and rollout-restart the gateway so it re-reads the env (env from a Secret is
# injected at container start and is not live-reloaded).
GATEWAY_SECRET="gateway-api-keys"
DEV_API_KEY="test-key"

# --- locate repo root (script lives in <root>/scripts) ----------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
    if kubectl --context "$K8S_CONTEXT" -n "$ns" wait \
        --for=condition=Ready pod -l "$selector" --timeout=5s >/dev/null 2>&1; then
      log "ready: -n ${ns} -l ${selector}"
      return 0
    fi
    if (( $(date +%s) > deadline )); then
      log "TIMEOUT waiting for: -n ${ns} -l ${selector}; current pods:"
      kubectl --context "$K8S_CONTEXT" -n "$ns" get pods -l "$selector" || true
      return 1
    fi
    sleep 5
  done
}

# wait_rollout <namespace> <resource> <timeout-seconds>
wait_rollout() {
  local ns="$1" resource="$2" timeout="$3"
  log "waiting (<=${timeout}s) for rollout: -n ${ns} ${resource}"
  kubectl --context "$K8S_CONTEXT" -n "$ns" rollout status "$resource" --timeout="${timeout}s"
}

# --- 0. sanity: required CLIs ----------------------------------------------
log "preflight: docker=${DOCKER_HOST}"
for bin in docker kind kubectl helm; do
  command -v "$bin" >/dev/null 2>&1 || die "required binary not found on PATH: $bin"
done
docker version --format 'docker server {{.Server.Version}}' >/dev/null 2>&1 \
  || die "cannot reach docker daemon at ${DOCKER_HOST}"

# --- 1. images (build only if missing) --------------------------------------
if docker image inspect "$MODEL_IMAGE" >/dev/null 2>&1; then
  log "image present: $MODEL_IMAGE"
else
  log "building $MODEL_IMAGE (make docker-build)"
  make docker-build
fi

if docker image inspect "$GATEWAY_IMAGE" >/dev/null 2>&1; then
  log "image present: $GATEWAY_IMAGE"
else
  log "building $GATEWAY_IMAGE"
  docker build -t "$GATEWAY_IMAGE" -f "$GATEWAY_DOCKERFILE" .
fi

# --- 2. kind cluster (skip if it already exists) ----------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log "kind cluster already exists: $CLUSTER_NAME (skipping create)"
else
  log "creating kind cluster: $CLUSTER_NAME"
  kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG" --wait 5m
fi

# Pin kubectl to this cluster's context for the rest of the run.
kubectl config use-context "$K8S_CONTEXT" >/dev/null
log "waiting for node Ready"
kubectl --context "$K8S_CONTEXT" wait --for=condition=Ready node --all --timeout=300s

# --- 3. nginx ingress controller --------------------------------------------
log "installing ingress-nginx (kind provider manifest)"
kubectl --context "$K8S_CONTEXT" apply -f "$INGRESS_MANIFEST_URL"
# The controller pod binds the node's :80/:443 via hostPort; the cluster's
# extraPortMappings publish those as host :8080/:8443.
wait_pods_ready "ingress-nginx" "app.kubernetes.io/component=controller" 300

# --- 4. kube-prometheus-stack (in-cluster Prometheus) -----------------------
log "adding helm repo: $PROM_HELM_REPO_NAME"
helm repo add "$PROM_HELM_REPO_NAME" "$PROM_HELM_REPO_URL" >/dev/null 2>&1 || true
helm repo update "$PROM_HELM_REPO_NAME" >/dev/null

PROM_VALUES_ARGS=()
if [[ -f "$PROM_VALUES_FILE" ]]; then
  log "using prometheus values file: $PROM_VALUES_FILE"
  PROM_VALUES_ARGS=(--values "$PROM_VALUES_FILE")
else
  log "no $PROM_VALUES_FILE — installing kube-prometheus-stack with default values (todo 32 adds values/alerts/dashboards)"
fi

log "installing kube-prometheus-stack (release: $PROM_HELM_RELEASE, ns: $PROM_NS)"
# No --wait: we poll the Prometheus pod ourselves with a bounded wait (helm
# --wait on this chart can trip over non-critical test/hook resources).
helm upgrade --install "$PROM_HELM_RELEASE" "$PROM_HELM_CHART" \
  -n "$PROM_NS" --create-namespace \
  --timeout 10m \
  "${PROM_VALUES_ARGS[@]}"

# The prometheus-operator must reconcile the Prometheus CR before the server
# pod exists; the poll loop tolerates that "no pods yet" window, so waiting on
# the Prometheus server pod is the meaningful gate (it cannot be Ready until the
# operator has done its job). Selector matches ONLY the server pod (the operator
# is labelled ...-prometheus-operator and node-exporter prometheus-node-exporter).
wait_pods_ready "$PROM_NS" "app.kubernetes.io/name=prometheus" 420
# Grafana ships with the stack; wait for it too (best evidence the stack is up).
wait_pods_ready "$PROM_NS" "app.kubernetes.io/name=grafana" 300 || \
  log "WARN: grafana not Ready yet (non-fatal for todo 19; Prometheus is the dependency)"

# --- 5. load images onto the kind node (no registry) ------------------------
log "kind load: $MODEL_IMAGE + $GATEWAY_IMAGE"
kind load docker-image "$MODEL_IMAGE" "$GATEWAY_IMAGE" --name "$CLUSTER_NAME"

# --- 6. apply the local overlay (namespace, configmap, deployments, ...) ----
log "applying overlay: k8s/overlays/local"
kubectl --context "$K8S_CONTEXT" apply -k k8s/overlays/local

# --- 7. gateway API-key secret (overwrite the example placeholder) ----------
# The overlay just applied secret.example.yaml (dev placeholder keys). Replace
# it with the local dev key so `X-API-Key: test-key` authenticates.
log "setting gateway API-key secret: $GATEWAY_SECRET (key: $DEV_API_KEY)"
kubectl --context "$K8S_CONTEXT" create secret generic "$GATEWAY_SECRET" \
  --from-literal=API_KEYS="$DEV_API_KEY" \
  -n "$NAMESPACE" --dry-run=client -o yaml \
  | kubectl --context "$K8S_CONTEXT" apply -f -

# Env from a Secret is baked in at container start, so restart the gateway to
# pick up the new key (the model-server does not read this secret).
log "rolling gateway to pick up the new API key"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" rollout restart deployment/gateway

# --- 8. wait for the app deployments ----------------------------------------
wait_rollout "$NAMESPACE" "deployment/model-server" 360
wait_rollout "$NAMESPACE" "deployment/gateway" 300

# --- 9. summary -------------------------------------------------------------
log "deploy complete — pod summary"
kubectl --context "$K8S_CONTEXT" get pods -n "$NAMESPACE"
kubectl --context "$K8S_CONTEXT" get pods -n "$PROM_NS"
kubectl --context "$K8S_CONTEXT" get pods -n ingress-nginx

cat <<EOF

InferenceOps local stack is up.
  Ingress (HTTP):  http://localhost:8080   (HTTPS: https://localhost:8443)
  The ingress host is inferenceops.local, so send the Host header, e.g.:

    curl -s http://localhost:8080/healthz -H 'Host: inferenceops.local'
    curl -s -X POST http://localhost:8080/v1/predict \\
      -H 'Host: inferenceops.local' -H 'Content-Type: application/json' \\
      -H 'X-API-Key: ${DEV_API_KEY}' \\
      -d '{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'

  (Host ports are 8080/8443 because rootless docker cannot bind <1024 unless
   sysctl net.ipv4.ip_unprivileged_port_start=0 — see infra/scripts/kind-config.yaml.)

  Teardown:  kind delete cluster --name ${CLUSTER_NAME}
EOF
