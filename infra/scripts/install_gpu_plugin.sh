#!/usr/bin/env bash
# ============================================================================
# install_gpu_plugin.sh — NVIDIA device-plugin DaemonSet (todo 25).
#
# Installs the NVIDIA k8s-device-plugin, which advertises `nvidia.com/gpu`
# resources on GPU nodes so the model-server (GPU overlay / the optional EKS
# GPU node group from todo 23) can request them.
#
# IDEMPOTENT: the DaemonSet is checked before install (a re-run skips the
# apply), then a bounded rollout wait confirms it reconciled. On a cluster
# with NO GPU nodes the DaemonSet schedules 0 pods (desiredNumberScheduled=0)
# and the rollout completes immediately — it simply waits for GPU nodes to
# join. Nothing here taints/labels nodes or touches non-GPU workloads.
#
# The kube context is taken from the first argument, else $KUBE_CONTEXT, else
# the local kind cluster context `kind-inferenceops`.
#
# Usage:
#   infra/scripts/install_gpu_plugin.sh
#   infra/scripts/install_gpu_plugin.sh kind-inferenceops
#   KUBE_CONTEXT=staging infra/scripts/install_gpu_plugin.sh
#
# Requires: kubectl (configured for the target context).
# ============================================================================
set -euo pipefail

# --- configuration ----------------------------------------------------------
KUBE_CONTEXT="${1:-${KUBE_CONTEXT:-kind-inferenceops}}"

GPU_PLUGIN_VERSION="v0.17.1"
GPU_PLUGIN_URL="https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/${GPU_PLUGIN_VERSION}/deployments/static/nvidia-device-plugin.yml"
GPU_PLUGIN_NS="kube-system"
GPU_PLUGIN_DS="nvidia-device-plugin-daemonset"

# --- locate repo root (script lives in <root>/infra/scripts) ----------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0. sanity --------------------------------------------------------------
log "gpu-plugin context: $KUBE_CONTEXT"
command -v kubectl >/dev/null 2>&1 || die "required binary not found on PATH: kubectl"
kubectl config get-contexts "$KUBE_CONTEXT" >/dev/null 2>&1 \
  || die "kube context not found: $KUBE_CONTEXT (is the cluster up?)"

# --- 1. install (check-before-apply) ----------------------------------------
if kubectl --context "$KUBE_CONTEXT" -n "$GPU_PLUGIN_NS" get daemonset "$GPU_PLUGIN_DS" >/dev/null 2>&1; then
  log "nvidia-device-plugin: already installed (skipping apply)"
else
  log "nvidia-device-plugin: installing ${GPU_PLUGIN_VERSION} from ${GPU_PLUGIN_URL}"
  kubectl --context "$KUBE_CONTEXT" apply -f "$GPU_PLUGIN_URL"
fi

# --- 2. wait for the DaemonSet to reconcile ---------------------------------
# With no GPU nodes present, desiredNumberScheduled=0 and this returns at once.
log "waiting (<=300s) for rollout: -n ${GPU_PLUGIN_NS} daemonset/${GPU_PLUGIN_DS}"
if ! kubectl --context "$KUBE_CONTEXT" -n "$GPU_PLUGIN_NS" rollout status \
    "daemonset/${GPU_PLUGIN_DS}" --timeout=300s; then
  log "rollout did not complete; current DaemonSet status:"
  kubectl --context "$KUBE_CONTEXT" -n "$GPU_PLUGIN_NS" get daemonset "$GPU_PLUGIN_DS" || true
  die "nvidia-device-plugin rollout failed"
fi

# --- summary ----------------------------------------------------------------
log "nvidia-device-plugin reconciled on context $KUBE_CONTEXT"
kubectl --context "$KUBE_CONTEXT" -n "$GPU_PLUGIN_NS" get daemonset "$GPU_PLUGIN_DS"
gpu_nodes="$(kubectl --context "$KUBE_CONTEXT" get nodes \
  -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' 2>/dev/null \
  | grep -c . || true)"
log "nodes advertising nvidia.com/gpu: ${gpu_nodes:-0} (0 is expected until GPU nodes join)"
