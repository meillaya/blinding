#!/usr/bin/env bash
# llm_cpu_smoke.sh — end-to-end smoke for the CPU LLM path (todo 43).
#
# Proves the WHOLE chain on CPU (no GPU): builds + loads the gateway image (the
# LLM proxy routes), starts a SMALL host LLM (llama.cpp by default), points the
# in-cluster gateway at it, and drives the OpenAI surface THROUGH the gateway
# ingress — /v1/completions must return a non-empty completion, /v1/models must
# list the model. Then it tears everything down (restores the gateway env, stops
# the host LLM) so the cluster is left exactly as it was found.
#
# Networking: the gateway pod runs in kind (rootless docker); the LLM runs on
# the HOST. The pod reaches the host via the host's LAN IP (slirp4netns forwards
# to the host network) — discovered here and verified from a pod before use.
#
# Usage:
#   scripts/llm_cpu_smoke.sh                # full: build + load + start + smoke
#   SKIP_BUILD=1 scripts/llm_cpu_smoke.sh   # reuse the already-loaded gateway
#   LLM_BACKEND=ollama scripts/llm_cpu_smoke.sh
#
# Writes a PASS/FAIL transcript to QA_FILE (default /tmp/qa/t43_llm.txt).
set -euo pipefail

export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1000/docker-me.sock}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-inferenceops}"
NAMESPACE="${NAMESPACE:-inferenceops}"
KIND_CLUSTER="${KIND_CLUSTER:-inferenceops}"
KIND_NODE="${KIND_NODE:-inferenceops-control-plane}"
GATEWAY_IMAGE="${GATEWAY_IMAGE:-inferenceops/gateway:dev}"
INGRESS_URL="${INGRESS_URL:-http://localhost:8080}"
INGRESS_HOST="${INGRESS_HOST:-inferenceops.local}"
LLM_BACKEND="${LLM_BACKEND:-llama-cpu}"
LLM_PORT="${LLM_PORT:-8001}"
SKIP_BUILD="${SKIP_BUILD:-0}"
MIN_AVAIL_MEM_GI="${MIN_AVAIL_MEM_GI:-10}"
QA_FILE="${QA_FILE:-/tmp/qa/t43_llm.txt}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RC=0
TEARDOWN_DONE=0

k() { kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" "$@"; }

log() { printf '\n=== %s ===\n' "$*"; }

host_ip() {
  # The host's outbound LAN IP — reachable from a kind pod under slirp4netns.
  if [ -n "${LLM_HOST_IP:-}" ]; then echo "$LLM_HOST_IP"; return 0; fi
  local ip
  ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}')"
  if [ -z "$ip" ]; then
    ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
  fi
  echo "$ip"
}

check_mem() {
  local avail_gi
  avail_gi="$(free -g | awk '/^Mem:/ {print $7}')"
  if [ "${avail_gi:-0}" -lt "$MIN_AVAIL_MEM_GI" ]; then
    echo "ERROR: only ${avail_gi}Gi available (< ${MIN_AVAIL_MEM_GI}Gi); refusing to start the CPU LLM" >&2
    return 1
  fi
  echo "host memory ok: ${avail_gi}Gi available"
}

build_and_load_gateway() {
  if [ "$SKIP_BUILD" = "1" ]; then
    log "SKIP_BUILD=1: reusing loaded gateway image $GATEWAY_IMAGE"
    return 0
  fi
  log "building gateway image $GATEWAY_IMAGE"
  docker build -t "$GATEWAY_IMAGE" -f apps/gateway/Dockerfile "$REPO_ROOT"
  log "loading gateway image into kind ($KIND_CLUSTER)"
  if ! kind load docker-image "$GATEWAY_IMAGE" --name "$KIND_CLUSTER"; then
    echo "kind load failed; falling back to ctr import"
    docker save "$GATEWAY_IMAGE" \
      | docker exec -i "$KIND_NODE" ctr -n k8s.io images import -
  fi
}

start_llm() {
  log "starting host LLM (backend=$LLM_BACKEND, port=$LLM_PORT)"
  LLM_BACKEND="$LLM_BACKEND" LLM_PORT="$LLM_PORT" "$REPO_ROOT/scripts/run_llm_cpu.sh" start
}

verify_pod_reaches_llm() {
  local ip="$1"
  local pod
  pod="$(k get pod -l app.kubernetes.io/name=gateway -o jsonpath='{.items[0].metadata.name}')"
  log "verifying gateway pod $pod can reach http://$ip:$LLM_PORT/v1/models"
  k exec "$pod" -- python -c "import urllib.request,sys
try:
    body = urllib.request.urlopen('http://$ip:$LLM_PORT/v1/models', timeout=8).read()
    print('pod->host LLM OK:', body[:120])
except Exception as exc:  # noqa: BLE001
    print('pod->host LLM FAIL:', exc, file=sys.stderr); sys.exit(1)"
}

configure_gateway() {
  local ip="$1"
  log "pointing gateway at LLM_BACKEND=$LLM_BACKEND LLM_URL=http://$ip:$LLM_PORT"
  k set env deployment/gateway "LLM_BACKEND=$LLM_BACKEND" "LLM_URL=http://$ip:$LLM_PORT"
  wait_gateway_settled
}

wait_gateway_settled() {
  # `rollout status` returns once the NEW replicas are ready and the old ones are
  # scaled down, but an old pod still serves during its ~6s graceful-shutdown
  # window (Terminating). Querying in that window can hit the OLD pod (no LLM
  # env) and read a stale /v1/models — so also wait until NO gateway pod carries
  # a deletionTimestamp (i.e. none are Terminating), bounded.
  log "waiting for gateway rollout to settle (new pods ready, old pods drained)"
  k rollout status deployment/gateway --timeout=150s
  local deadline=$((SECONDS + 60))
  local terminating
  while [ "$SECONDS" -lt "$deadline" ]; do
    terminating="$(k get pods -l app.kubernetes.io/name=gateway \
      -o jsonpath='{.items[*].metadata.deletionTimestamp}')"
    if [ -z "$terminating" ]; then
      echo "gateway settled: all pods ready, none Terminating"
      return 0
    fi
    echo "gateway still draining a Terminating pod; waiting..."
    sleep 2
  done
  echo "ERROR: gateway pods still Terminating after 60s" >&2
  k get pods -l app.kubernetes.io/name=gateway >&2 || true
  return 1
}

# shellcheck disable=SC2329  # invoked by teardown(), which runs via the EXIT trap
restore_gateway() {
  log "restoring gateway env (removing LLM_BACKEND/LLM_URL)"
  k set env deployment/gateway LLM_BACKEND- LLM_URL- || true
  k rollout status deployment/gateway --timeout=150s || true
}

gateway_curl() {
  local method="$1" path="$2" api_key="$3" data="${4:-}"
  local -a args=(-s --max-time 90 -X "$method" "$INGRESS_URL$path"
    -H "Host: $INGRESS_HOST" -H "X-API-Key: $api_key")
  if [ -n "$data" ]; then
    args+=(-H 'Content-Type: application/json' -d "$data")
  fi
  curl "${args[@]}"
}

smoke_requests() {
  local api_key
  api_key="$(k exec deployment/gateway -- printenv API_KEYS | cut -d, -f1)"
  echo "using gateway API key: ${api_key:0:12}..."

  log "POST /v1/completions (through the gateway)"
  local completions
  completions="$(gateway_curl POST /v1/completions "$api_key" \
    '{"model":"small-model","prompt":"Hello","max_tokens":16}')"
  echo "$completions"
  echo "$completions" | python3 -c "import json,sys
body = json.load(sys.stdin)
text = body['choices'][0]['text']
assert text.strip(), 'empty completion text'
print('PASS: /v1/completions returned non-empty text:', repr(text))"

  log "POST /v1/chat/completions (through the gateway)"
  local chat
  chat="$(gateway_curl POST /v1/chat/completions "$api_key" \
    '{"model":"small-model","messages":[{"role":"user","content":"Hi"}],"max_tokens":16}')"
  echo "$chat"
  echo "$chat" | python3 -c "import json,sys
body = json.load(sys.stdin)
content = body['choices'][0]['message']['content']
print('PASS: /v1/chat/completions returned content:', repr(content))"

  wait_for_llm_models "$api_key"
}

wait_for_llm_models() {
  # Bounded poll (not a one-shot): a stray request can still land on a stale pod
  # mid-drain and read the model-server's /v1/models, so retry until the LLM's
  # small-model actually appears rather than failing on the first stale read.
  local api_key="$1"
  log "GET /v1/models (bounded poll until small-model appears)"
  local deadline=$((SECONDS + 60))
  local models=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    models="$(gateway_curl GET /v1/models "$api_key" || true)"
    if printf '%s' "$models" | python3 -c "import json,sys
try:
    body = json.load(sys.stdin)
except Exception:  # noqa: BLE001
    sys.exit(1)
ids = [m.get('id') or m.get('model') or m.get('name') for m in body.get('data', body.get('models', []))]
sys.exit(0 if 'small-model' in ids else 1)"; then
      echo "$models"
      echo "PASS: /v1/models lists small-model"
      return 0
    fi
    sleep 2
  done
  echo "ERROR: /v1/models did not list small-model within 60s" >&2
  echo "last response: $models" >&2
  return 1
}

# shellcheck disable=SC2329  # invoked indirectly via `trap teardown EXIT`
teardown() {
  if [ "$TEARDOWN_DONE" = "1" ]; then return 0; fi
  TEARDOWN_DONE=1
  log "TEARDOWN"
  restore_gateway
  LLM_BACKEND="$LLM_BACKEND" LLM_PORT="$LLM_PORT" "$REPO_ROOT/scripts/run_llm_cpu.sh" stop || true
  if [ "$RC" -eq 0 ]; then
    echo "SMOKE PASS (qa: $QA_FILE)"
  else
    echo "SMOKE FAIL (qa: $QA_FILE)"
  fi
}

main() {
  echo "# todo 43 llm-cpu-smoke transcript ($(date -Is))"
  check_mem
  build_and_load_gateway
  start_llm

  local ip
  ip="$(host_ip)"
  if [ -z "$ip" ]; then
    echo "ERROR: could not determine a host IP reachable from the cluster" >&2
    return 1
  fi
  echo "host IP (as seen from the cluster): $ip"
  verify_pod_reaches_llm "$ip"
  configure_gateway "$ip"
  smoke_requests

  log "RESULT"
  echo "PASS: gateway /v1/completions + /v1/chat/completions + /v1/models served by the CPU LLM ($LLM_BACKEND)"
  RC=0
}

mkdir -p "$(dirname "$QA_FILE")"
trap teardown EXIT
# Tee the whole run to the QA transcript. main runs under `set -e`: the first
# failing phase aborts it (RC stays 1) and the EXIT trap tears down + reports.
exec > >(tee -a "$QA_FILE") 2>&1
main
exit "$RC"
