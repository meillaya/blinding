#!/usr/bin/env bash
# ============================================================================
# _lib.sh — shared helpers for the failure-scenario suite (todo 49).
#
# SOURCED by every scenario script (crash_model_server.sh, bad_model_deploy.sh,
# latency_regression.sh, traffic_spike.sh) and by run_failure_scenarios.sh. It is
# a library, NOT a standalone program: it defines configuration, logging, the
# cluster-stable memory/D-state guards, gateway/predict probes, HPA helpers and a
# base-state restore/verify pair. It has no `main` and runs nothing on its own.
#
# The SC2034/SC2329 file-disable below is deliberate: shellcheck analyses this
# file in isolation, but its variables and functions are consumed by the SOURCING
# scripts, not within this file (the same convention canary_check.sh uses for its
# trap-invoked functions). Run `shellcheck <scenario>.sh` to analyse a scenario
# together with this library via the `# shellcheck source=_lib.sh` directive.
# ============================================================================
# shellcheck disable=SC2034,SC2329  # sourced library: vars+funcs used by the sourcing scripts, not here

# Docker/kind run ROOTLESS on a user socket -> every docker/kind call needs this.
export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/1000/docker-me.sock}"

CLUSTER_NAME="inferenceops"
K8S_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
NAMESPACE="${NAMESPACE:-inferenceops}"
INGRESS_HOST="inferenceops.local"
INGRESS_PORT="${INGRESS_PORT:-8080}"

# Model-version labels (the canary split + rollback discriminator). The stable
# tier serves 1.0.0; the candidate artifact carries 2.0.0.
STABLE_VERSION="1.0.0"
CANDIDATE_VERSION="2.0.0"

STABLE_ARTIFACT_DIR="models/artifacts/fraud_detector"
CANARY_ARTIFACT_DIR="models/artifacts/fraud_detector_canary"

# Cluster-stable guard: refuse to START a scenario when host available memory is
# below this many whole GiB (the swap-thrash lesson — see learnings). Bounded
# scenarios still monitor in-flight; this is the pre-flight gate.
MIN_AVAIL_GIB="${MIN_AVAIL_GIB:-10}"

# 20-feature fraud-detector predict body (probe payload only).
PREDICT_BODY='{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'

# Repo layout: these scripts live in <root>/scripts/failure_scenarios, so the
# repo root is two levels up from THIS file (which sits beside the scenarios).
FAILURE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${FAILURE_DIR}/../.." && pwd)"

# --- logging ----------------------------------------------------------------
log()    { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
banner() { printf '\n\033[1;35m==== %s ====\033[0m\n' "$*"; }
warn()   { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }
err()    { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; }
die()    { err "$@"; exit 1; }
# ok <msg> — a green PASS/verification line (the evidence the QA greps for).
ok()     { printf '\033[1;32m%s\033[0m\n' "$*"; }

# --- preflight --------------------------------------------------------------
require_bins() {  # <bin...> — die unless every named binary is on PATH.
  local b
  for b in "$@"; do
    command -v "$b" >/dev/null 2>&1 || die "required binary not found on PATH: $b"
  done
}

require_cluster() {  # die unless the inferenceops namespace is reachable.
  kubectl --context "$K8S_CONTEXT" get ns "$NAMESPACE" >/dev/null 2>&1 \
    || die "namespace ${NAMESPACE} not found on context ${K8S_CONTEXT} (run scripts/deploy_local.sh first)"
}

# --- memory / D-state guards (cluster-stable-aware) -------------------------
avail_gib()    { free -g | awk '/^Mem:/{print $7}'; }
# pgrep matches process NAMES, not states, so it cannot count D-state (uninterruptible
# I/O wait) processes — `ps -eo state=` + grep '^D' is the only correct way (the
# swap-thrash guard).
# shellcheck disable=SC2009  # counting D-state processes; pgrep has no state filter
dstate_count() { ps -eo state= | grep -c '^D' || true; }

# memory_guard — pre-flight gate: refuse to start when available memory is low.
memory_guard() {
  local avail
  avail="$(avail_gib)"
  if ! [[ "$avail" =~ ^[0-9]+$ ]]; then
    warn "could not read available memory; continuing without the guard"
    return 0
  fi
  if (( avail < MIN_AVAIL_GIB )); then
    die "host available memory ${avail}Gi < ${MIN_AVAIL_GIB}Gi — refusing to start (swap-thrash guard)"
  fi
  log "memory guard: ${avail}Gi available (>= ${MIN_AVAIL_GIB}Gi required), D-state $(dstate_count) — OK"
}

# --- gateway / predict probes -----------------------------------------------
# gateway_api_key — the live gateway's first API key (read from the running pod
# env, NOT the example secret). Empty (not fatal) if the gateway is absent.
gateway_api_key() {
  kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" exec deploy/gateway \
    -- printenv API_KEYS 2>/dev/null | cut -d, -f1 || true
}

# predict [extra-curl-args...] — POST /v1/predict through the ingress host.
# Callers add `-H 'X-API-Key: <key>'` for the base (gateway) path; during a
# canary the gateway is bypassed, so the Host header alone suffices.
predict() {
  curl -s --max-time 5 -X POST "http://localhost:${INGRESS_PORT}/v1/predict" \
    -H "Host: ${INGRESS_HOST}" -H 'Content-Type: application/json' \
    -d "$PREDICT_BODY" "$@" 2>/dev/null || true
}

# --- HPA helpers ------------------------------------------------------------
hpa_replicas() {  # current model-server replica count (empty if unreadable).
  kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get hpa model-server \
    -o jsonpath='{.status.currentReplicas}' 2>/dev/null || true
}

# --- cluster restore / stability verification -------------------------------
# restore_base — idempotent: remove any canary, stop the check loop, restore the
# base ingress (-> service-gateway). Safe to call from any state.
restore_base() {
  ( cd "$ROOT" && scripts/rollback.sh >/dev/null 2>&1 ) || true
}

ingress_backend() {  # service the base `inferenceops` ingress points at.
  kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get ingress inferenceops \
    -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}' 2>/dev/null || true
}

canary_present() {  # 0 if the canary Deployment exists, non-zero otherwise.
  kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get deployment model-server-canary \
    >/dev/null 2>&1
}

# verify_stable — assert the cluster is at base: ingress -> service-gateway, no
# orphan canary, HPA >= 2 replicas, model-server pods listed. Prints evidence and
# returns non-zero on any breach (so callers can `|| die`).
verify_stable() {
  local backend replicas
  backend="$(ingress_backend)"
  replicas="$(hpa_replicas)"
  if [[ "$backend" != "service-gateway" ]]; then
    err "ingress backend is '${backend}', expected service-gateway"
    return 1
  fi
  if canary_present; then
    err "orphan canary deployment (model-server-canary) still present"
    return 1
  fi
  if ! [[ "$replicas" =~ ^[0-9]+$ ]] || (( replicas < 2 )); then
    err "HPA currentReplicas '${replicas}' is not >= 2"
    return 1
  fi
  kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get pods \
    -l app.kubernetes.io/name=model-server \
    -o jsonpath='{range .items[*]}  {.metadata.name}{"  "}{.status.phase}{"  restarts="}{.status.containerStatuses[0].restartCount}{"\n"}{end}' \
    2>/dev/null || true
  ok "STABLE: ingress->service-gateway, no canary, HPA replicas=${replicas}"
  return 0
}

# report_alerts — best-effort, INFORMATIONAL: list currently FIRING Prometheus
# alerts. Never fails the caller — an unreachable Prometheus just prints a note.
# Used by the crash scenario for its "alert fires if applicable" evidence (a
# transient single-pod crash that self-heals in seconds is NOT expected to fire
# the sustained HighP95Latency/HighErrorRate alerts, which carry `for` windows).
report_alerts() {
  local pf_log pf_pid local_port _ url firing
  pf_log="$(mktemp /tmp/inferenceops-alerts-pf.XXXXXX)"
  kubectl --context "$K8S_CONTEXT" -n monitoring port-forward \
    svc/kube-prometheus-kube-prome-prometheus :9090 >"$pf_log" 2>&1 &
  pf_pid=$!
  local_port=""
  for _ in $(seq 1 30); do
    # `|| true`: grep exits 1 (pipefail) before the port line exists; the callers
    # run under `set -e`, so an unguarded no-match here would abort the script.
    local_port="$(grep -oE '127\.0\.0\.1:[0-9]+' "$pf_log" 2>/dev/null | head -1 | cut -d: -f2 || true)"
    [[ -n "$local_port" ]] && break
    kill -0 "$pf_pid" 2>/dev/null || break
    sleep 0.3
  done
  if [[ -z "$local_port" ]]; then
    printf '  alerts: Prometheus port-forward unavailable — alert state not queried\n'
    kill "$pf_pid" 2>/dev/null || true
    rm -f "$pf_log"
    return 0
  fi
  url="http://localhost:${local_port}"
  firing="$(curl -sS --max-time 8 "${url}/api/v1/alerts" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    names = sorted({a["labels"].get("alertname", "?")
                    for a in d.get("data", {}).get("alerts", [])
                    if a.get("state") == "firing"})
    print(", ".join(names) if names else "none firing")
except Exception:
    print("unparseable")
' 2>/dev/null || echo "query-failed")"
  printf '  alerts (Prometheus /api/v1/alerts): %s\n' "$firing"
  kill "$pf_pid" 2>/dev/null || true
  rm -f "$pf_log"
  return 0
}
