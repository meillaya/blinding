#!/usr/bin/env bash
# install_kserve.sh — install KServe 0.18 (Standard mode, no Istio/Knative) (todo 44).
#
# Install ORDER is the whole point of this script:
#
#   1. cert-manager (pinned v1.15.3) FIRST. KServe's admission webhooks serve
#      TLS from a cert-manager-issued cert (the Issuer + Certificate CRs ship
#      inside kserve.yaml). Skip this step and those CRs fail to apply
#      (unknown kind "cert-manager.io/v1, Kind=Certificate"), the webhook
#      serving-cert Secret never appears, kserve-controller-manager never
#      starts (CreateContainerConfigError on the missing secret mount), and
#      EVERY `kubectl apply` of an InferenceService is then REJECTED by the
#      apiserver because the webhook has failurePolicy: Fail. That failure is
#      demonstrated in /tmp/qa/t44_kserve_fail.txt.
#   2. KServe v0.18.0 via `kubectl apply --server-side` — the bundled CRDs
#      exceed the client-side-apply last-applied-configuration annotation
#      limit, so server-side apply is the documented install path. Requires
#      K8s 1.32+ (this cluster: v1.35).
#
# "Standard mode" is a per-InferenceService property, not a different control
# plane: the ISVCs in serving/kserve/ carry the annotation
# `serving.kserve.io/deploymentMode: Standard` and render as plain
# Deployment + Service (+ KServe-managed HPA) with NO Knative or Istio
# dependency — which is exactly why this script installs NEITHER. The cluster
# default mode stays Serverless, so the annotation is mandatory on ISVCs.
#
# Idempotent / re-runnable: every write is a declarative apply and the waits
# converge to the same healthy state.
set -euo pipefail

# --- configuration -----------------------------------------------------------
CLUSTER_NAME="inferenceops"
K8S_CONTEXT="${K8S_CONTEXT:-kind-${CLUSTER_NAME}}"
CERT_MANAGER_VERSION="v1.15.3"
KSERVE_VERSION="v0.18.0"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-300s}"

kube() { kubectl --context "${K8S_CONTEXT}" "$@"; }

# --- 1. cert-manager FIRST (issues the KServe webhook's TLS cert) -------------
kube apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
# All three deployments must be Available; the WEBHOOK is the hard dependency
# (it admits the Certificate CRs kserve.yaml ships in step 2).
kube -n cert-manager wait --for=condition=Available --timeout="${WAIT_TIMEOUT}" \
  deployment/cert-manager \
  deployment/cert-manager-cainjector \
  deployment/cert-manager-webhook

# --- 2. KServe control plane + cluster RBAC (NO Knative, NO Istio) ------------
# The v0.18.0 release manifest carries `namespace: kserve` metadata but NO
# Namespace object (the Helm path uses --create-namespace), so create it here.
kube create namespace kserve --dry-run=client -o yaml | kube apply -f -

# kserve.yaml ships CRDs AND instances of them (ClusterStorageContainer) in one
# file; on a FRESH cluster kubectl's discovery cache predates the just-applied
# CRDs, so the instance can fail mapping on the first pass. Bounded re-apply:
# once the CRDs are Established the second pass converges fully (idempotent).
kserve_manifest="https://github.com/kserve/kserve/releases/download/${KSERVE_VERSION}/kserve.yaml"
for attempt in 1 2 3; do
  if kube apply --server-side -f "${kserve_manifest}"; then
    break
  fi
  echo "kserve.yaml apply incomplete (attempt ${attempt}/3 — CRD discovery race); settling..." >&2
  sleep 5
  if [ "${attempt}" -eq 3 ]; then
    echo "FATAL: kserve.yaml did not apply cleanly after 3 attempts" >&2
    exit 1
  fi
done
kube apply --server-side -f "https://github.com/kserve/kserve/releases/download/${KSERVE_VERSION}/kserve-cluster-resources.yaml"
kube -n kserve wait --for=condition=Available --timeout="${WAIT_TIMEOUT}" \
  deployment/kserve-controller-manager

echo "OK: cert-manager ${CERT_MANAGER_VERSION} + KServe ${KSERVE_VERSION} installed on ${K8S_CONTEXT}"
echo "    Standard mode (no Istio/Knative) — deploy serving/kserve/*.yaml next."
