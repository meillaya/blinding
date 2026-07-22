#!/usr/bin/env bash
# ============================================================================
# deploy_canary.sh — deploy a canary model version at a 90/10 nginx split
# (todo 29). Wired to `make deploy-canary MODEL_VERSION=<v>`.
#
# Topology (Oracle F2 — see serving/fastapi/deployment.md):
#   BASE:    ingress(inferenceops) -> service-gateway -> service-model-server
#   CANARY:  the canary `stable-ingress` REPLACES the base ingress for host
#            inferenceops.local (same object name `inferenceops`, so apply
#            overwrites it) and fronts the model-server tier DIRECTLY:
#              stable-ingress(inferenceops)        -> service-model-server  (90%)
#              canary-ingress(inferenceops-canary) -> service-model-server-canary (10%)
#            The gateway (auth/rate-limit) is BYPASSED during the canary window
#            so the split happens cleanly at the model-server tier; rollback /
#            promote reinstate the base ingress (-> gateway).
#
# What this script does (idempotent / re-runnable):
#   1. resolve + validate the candidate MODEL_VERSION (arg, else the candidate
#      artifact's model_meta.json),
#   2. bake the candidate artifact (models/artifacts/fraud_detector_canary/) into
#      the inferenceops/model-server:dev image and `kind load` it, so a canary pod
#      started with MODEL_VERSION=<v> can load it and become Ready,
#   3. apply the canary Deployment+Service, the canary Ingress (weight 10) and the
#      stable Ingress (replacing the base ingress for the host),
#   4. pin MODEL_VERSION on the canary Deployment and wait for it to roll out.
#
# The committed apps/model-server/Dockerfile is NOT modified: the image is
# augmented at deploy time from a throwaway Dockerfile (FROM the current dev
# image + one COPY), so the base build stays untouched.
#
# Docker/kind run ROOTLESS on a user socket -> every docker/kind call needs
# DOCKER_HOST (defaulted here; the Makefile also exports it).
#
# Usage:
#   scripts/deploy_canary.sh 2.0.0
#   make deploy-canary MODEL_VERSION=2.0.0
# ============================================================================
set -euo pipefail

# --- configuration ----------------------------------------------------------
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1000/docker-me.sock}"

CLUSTER_NAME="inferenceops"
K8S_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
NAMESPACE="${NAMESPACE:-inferenceops}"

MODEL_IMAGE="inferenceops/model-server:dev"
STABLE_ARTIFACT_DIR="models/artifacts/fraud_detector"
CANARY_ARTIFACT_DIR="models/artifacts/fraud_detector_canary"
CANARY_DEPLOY="model-server-canary"
CANARY_INGRESS="inferenceops-canary"
CANARY_WEIGHT_DEFAULT="10"

# todo 30 (automated rollback) starts a canary_check.sh loop from this script and
# records it in ${TMPDIR:-/tmp}/inferenceops-canary-check.pid; promote.sh /
# rollback.sh stop it via that same convention. todo 29 starts no loop, so this
# script does not touch the pidfile (it is absent unless todo 30 created it).

# --- locate repo root (script lives in <root>/scripts) ----------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# json_field <file> <key> — read a top-level string field from a JSON object.
# Uses python3 (always present in this repo) so we do not depend on jq here.
json_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

# load_image_to_node <image> — get <image> onto every node of the kind cluster.
# Prefers `kind load`; some kind/containerd pairs (e.g. kind v0.31 + containerd
# 2.3) fail `kind load` with "unknown containerd config version", so on failure
# fall back to streaming the image straight into the node's containerd (k8s.io
# namespace, which the kubelet reads) via `docker save | ctr images import`.
load_image_to_node() {
  local image="$1" node
  if kind load docker-image "$image" --name "$CLUSTER_NAME" >/dev/null 2>&1; then
    log "kind load: $image -> cluster $CLUSTER_NAME"
    return 0
  fi
  log "kind load unavailable (kind/containerd mismatch); falling back to ctr import"
  for node in $(docker ps --filter "label=io.x-k8s.kind.cluster=${CLUSTER_NAME}" --format '{{.Names}}'); do
    docker save "$image" | docker exec -i "$node" ctr --namespace k8s.io images import - >/dev/null
  done
  log "ctr import: $image -> cluster $CLUSTER_NAME"
}

# --- 0. preflight -----------------------------------------------------------
for bin in docker kind kubectl python3; do
  command -v "$bin" >/dev/null 2>&1 || die "required binary not found on PATH: $bin"
done
docker version --format 'docker server {{.Server.Version}}' >/dev/null 2>&1 \
  || die "cannot reach docker daemon at ${DOCKER_HOST}"
kubectl --context "$K8S_CONTEXT" get ns "$NAMESPACE" >/dev/null 2>&1 \
  || die "namespace ${NAMESPACE} not found on context ${K8S_CONTEXT} (run scripts/deploy_local.sh first)"

# --- 1. resolve + validate the candidate version ----------------------------
[[ -f "${CANARY_ARTIFACT_DIR}/model_meta.json" ]] \
  || die "candidate artifact missing: ${CANARY_ARTIFACT_DIR}/model_meta.json"

# The candidate model.pkl is a generated binary blob (gitignored, like the stable
# one). Ensure it exists: copy the stable trained model if the candidate's is
# absent (the routing demo reuses the stable weights; only the version label
# differs). If the stable artifact is also missing, training must run first.
if [[ ! -f "${CANARY_ARTIFACT_DIR}/model.pkl" ]]; then
  [[ -f "${STABLE_ARTIFACT_DIR}/model.pkl" ]] \
    || die "candidate ${CANARY_ARTIFACT_DIR}/model.pkl absent and no stable \
${STABLE_ARTIFACT_DIR}/model.pkl to copy — run 'make train' first"
  log "materializing candidate artifact: cp ${STABLE_ARTIFACT_DIR}/model.pkl -> ${CANARY_ARTIFACT_DIR}/"
  cp "${STABLE_ARTIFACT_DIR}/model.pkl" "${CANARY_ARTIFACT_DIR}/model.pkl"
fi

ARTIFACT_VERSION="$(json_field "${CANARY_ARTIFACT_DIR}/model_meta.json" model_version)"
ARTIFACT_MODEL_ID="$(json_field "${CANARY_ARTIFACT_DIR}/model_meta.json" model_id)"
REQUESTED_VERSION="${1:-}"
CANDIDATE_VERSION="${REQUESTED_VERSION:-$ARTIFACT_VERSION}"

# The canary pod loads its artifact by (model_id, model_version) metadata, so the
# requested version MUST be the one the candidate artifact actually carries — a
# mismatch would leave the pod not-Ready (no such artifact) and the canary dead.
[[ "$CANDIDATE_VERSION" == "$ARTIFACT_VERSION" ]] \
  || die "requested MODEL_VERSION=${CANDIDATE_VERSION} but the candidate artifact \
(${CANARY_ARTIFACT_DIR}) is version ${ARTIFACT_VERSION}. Re-train/copy a candidate \
artifact with that version first (model versioning is decoupled from git semver)."

log "candidate: model_id=${ARTIFACT_MODEL_ID} model_version=${CANDIDATE_VERSION} (weight ${CANARY_WEIGHT_DEFAULT}%)"

# --- 2. bake the candidate artifact into the image + kind load --------------
# Build only the base image if it is absent; then augment it (FROM the dev tag,
# which BuildKit resolves to the LOCAL image + COPY the candidate artifact) into
# a temp tag, and re-tag it as dev — so BOTH the stable (1.0.0) and candidate
# (<v>) artifacts resolve from one image. (FROM by tag, not by bare sha256 ID:
# BuildKit treats `FROM sha256:<id>` as a remote pull and fails.)
if ! docker image inspect "$MODEL_IMAGE" >/dev/null 2>&1; then
  log "base image missing — building $MODEL_IMAGE (make docker-build)"
  make docker-build
fi

AUGMENTED_IMAGE="inferenceops/model-server:canary-augmented"
log "augmenting $MODEL_IMAGE with ${CANARY_ARTIFACT_DIR}/"

CANARY_DOCKERFILE="$(mktemp /tmp/inferenceops-canary-dockerfile.XXXXXX)"
trap 'rm -f "$CANARY_DOCKERFILE"' EXIT
cat > "$CANARY_DOCKERFILE" <<EOF
FROM ${MODEL_IMAGE}
COPY ${CANARY_ARTIFACT_DIR}/ /app/${CANARY_ARTIFACT_DIR}/
EOF
docker build -t "$AUGMENTED_IMAGE" -f "$CANARY_DOCKERFILE" .
# Re-tag dev -> augmented (the temp tag is then dropped; the image stays).
docker tag "$AUGMENTED_IMAGE" "$MODEL_IMAGE"
docker rmi "$AUGMENTED_IMAGE" >/dev/null

log "loading $MODEL_IMAGE onto the kind node"
load_image_to_node "$MODEL_IMAGE"

# --- 3. apply the canary manifests ------------------------------------------
# Order matters for a clean first-time split: the canary tier (Deployment+Service
# + canary Ingress) comes up first, then the stable Ingress SWAPS the base ingress
# (service-gateway -> service-model-server) so the host flips to the canary pair
# only once the candidate is deployable. apply is declarative/idempotent.
log "applying canary Deployment + Service"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" apply -f k8s/canary/canary-deployment.yaml

log "applying canary Ingress (${CANARY_INGRESS}, weight ${CANARY_WEIGHT_DEFAULT})"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" apply -f k8s/canary/canary-ingress.yaml

# --- 4. pin the candidate version + wait for rollout ------------------------
# The manifest defaults MODEL_VERSION to the candidate; set env makes the
# requested version authoritative (no-op when it already matches).
log "pinning ${CANARY_DEPLOY} MODEL_VERSION=${CANDIDATE_VERSION}"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" set env \
  "deployment/${CANARY_DEPLOY}" \
  "MODEL_ID=${ARTIFACT_MODEL_ID}" "MODEL_VERSION=${CANDIDATE_VERSION}"

log "waiting (<=300s) for ${CANARY_DEPLOY} rollout"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" rollout status \
  "deployment/${CANARY_DEPLOY}" --timeout=300s

# The canary pod must be Ready (candidate artifact loaded) before the host flips,
# otherwise the canary Service has no endpoints and its 10% would 503.
log "waiting (<=120s) for Ready canary pods"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" wait \
  --for=condition=Ready pod -l "app.kubernetes.io/name=${CANARY_DEPLOY}" --timeout=120s

# --- 5. swap the base ingress for the canary stable-ingress -----------------
# Same object name (`inferenceops`) as the base ingress -> apply overwrites it in
# place (service-gateway -> service-model-server). The gateway is now bypassed.
log "swapping base ingress -> canary stable-ingress (service-model-server)"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" apply -f k8s/canary/stable-ingress.yaml

# --- 6. verify the canary pair ----------------------------------------------
log "verifying canary Ingress annotations + shared host"
CANARY_JSON="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get ingress "$CANARY_INGRESS" -o json)"
python3 - "$CANARY_JSON" <<'PY'
import json, sys
ing = json.loads(sys.argv[1])
ann = ing["metadata"].get("annotations", {})
host = ing["spec"]["rules"][0]["host"]
assert ann.get("nginx.ingress.kubernetes.io/canary") == "true", "canary annotation missing"
assert ann.get("nginx.ingress.kubernetes.io/canary-weight") == "10", "canary-weight != 10"
assert host == "inferenceops.local", f"unexpected host {host}"
print(f"PASS: {ing['metadata']['name']} canary=true weight=10 host={host}")
PY

STABLE_BACKEND="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get ingress inferenceops \
  -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}')"
[[ "$STABLE_BACKEND" == "service-model-server" ]] \
  || die "stable ingress backend is ${STABLE_BACKEND}, expected service-model-server"
log "stable ingress (inferenceops) -> ${STABLE_BACKEND}"

cat <<EOF

Canary deployed (90/10 split at the model-server tier; gateway bypassed).
  stable  (90%): service-model-server         model_version=1.0.0
  canary  (10%): service-model-server-canary  model_version=${CANDIDATE_VERSION}

  NOTE: the gateway (auth/rate-limit) is bypassed during the canary window, so
  /v1/predict needs NO X-API-Key right now. Send the Host header only:

    curl -s -X POST http://localhost:8080/v1/predict \\
      -H 'Host: inferenceops.local' -H 'Content-Type: application/json' \\
      -d '{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'

  Promote (100% candidate):  make promote
  Rollback (0% + base):      make rollback
EOF
