#!/usr/bin/env bash
# ============================================================================
# crash_model_server.sh — FAILURE SCENARIO 1/4 (todo 49): model-server crash.
#
# Crashes a model-server pod and proves the platform SELF-HEALS: the container
# is restarted in place by the kubelet (RestartPolicy=Always) — the SAME recovery
# path a liveness-probe failure converges to (a process hung past /healthz's
# failureThreshold is killed by the kubelet and restarted identically). Evidence:
#   1. RESTARTS increments on the SAME pod (an in-place container restart, not a
#      reschedule — `kubectl delete pod` would create a NEW pod at restartCount 0),
#   2. the pod returns to Ready,
#   3. the service keeps serving /v1/predict (model_version=1.0.0) throughout,
#   4. alert state is reported (informational — a transient single-pod crash that
#      self-heals in seconds is NOT expected to fire the sustained HighP95Latency/
#      HighErrorRate alerts, which carry multi-minute `for` windows).
#
# Crash mechanism: `kubectl exec <pod> -- sh -c 'kill 1'`. PID 1 is uvicorn
# (exec-form CMD in apps/model-server/Dockerfile); SIGTERM → graceful shutdown →
# container exit → kubelet restart. `kill -9 1` does NOT work (the kernel shields
# PID 1 from unhandled signals), so we use SIGTERM. The container runs as uid 1000
# (appuser), which may signal same-uid PID 1. (Validated in todo 38's soak QA.)
#
# Cluster-stable-aware: pre-flight memory guard; the scenario mutates nothing
# declarative (no manifest apply) — the crash self-heals, and the EXIT-free design
# leaves the cluster exactly as found (base ingress, HPA at 2, no orphan objects).
#
# Usage:  scripts/failure_scenarios/crash_model_server.sh
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
# shellcheck disable=SC1091  # lib path is runtime-resolved (${HERE}); analyse the combined program with `shellcheck -x`
source "${HERE}/_lib.sh"
cd "$ROOT" || exit 1

# Bounded waits (the kubelet restart + model reload takes ~10-20s on kind).
RESTART_WAIT="${RESTART_WAIT:-120}"  # seconds to wait for restartCount++
READY_WAIT="${READY_WAIT:-120}"      # seconds to wait for Ready after restart

require_bins kubectl curl python3
require_cluster
memory_guard

banner "FAILURE SCENARIO 1/4 — model-server crash → kubelet self-heal (liveness-probe path)"
printf '  Recovery contract: crash a pod → kubelet restarts the container in place\n'
printf '  (RestartPolicy=Always) → RESTARTS increments → pod Ready → service serves.\n'

# --- 1. pick a target pod + record its restartCount -------------------------
POD="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get pods \
  -l app.kubernetes.io/name=model-server \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
[[ -n "$POD" ]] || die "no model-server pod found in ${NAMESPACE}"

BEFORE="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get pod "$POD" \
  -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)"
[[ "$BEFORE" =~ ^[0-9]+$ ]] || BEFORE=0
log "target pod: ${POD} (restartCount=${BEFORE})"

# --- 2. confirm the service serves BEFORE the crash -------------------------
KEY="$(gateway_api_key)"
[[ -n "$KEY" ]] || die "could not read the live gateway API key (gateway down?)"
RESP="$(predict -H "X-API-Key: ${KEY}")"
[[ "$RESP" == *"\"model_version\":\"${STABLE_VERSION}\""* ]] \
  || die "pre-crash predict did not serve model_version=${STABLE_VERSION}: ${RESP}"
ok "pre-crash: serving model_version=${STABLE_VERSION} via the gateway"

# --- 3. crash the pod (kill PID 1 = uvicorn) --------------------------------
log "crashing ${POD}: kubectl exec ... 'kill 1' (SIGTERM uvicorn → container exit)"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" exec "$POD" -- sh -c 'kill 1' \
  || die "failed to signal PID 1 in ${POD}"

# --- 4. wait for RESTARTS to increment on the SAME pod ----------------------
log "waiting (<= ${RESTART_WAIT}s) for restartCount to increment on ${POD}"
WAITED=0
NOW="$BEFORE"
while true; do
  NOW="$(kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" get pod "$POD" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo "$BEFORE")"
  [[ "$NOW" =~ ^[0-9]+$ ]] || NOW="$BEFORE"
  if (( NOW > BEFORE )); then
    ok "RESTARTS incremented: ${BEFORE} → ${NOW} on ${POD} (in-place container restart)"
    break
  fi
  if (( WAITED >= RESTART_WAIT )); then
    die "restartCount did not increment within ${RESTART_WAIT}s (stuck at ${NOW}) — pod did NOT self-heal"
  fi
  sleep 3
  WAITED=$(( WAITED + 3 ))
done

# --- 5. wait for the pod to become Ready again ------------------------------
log "waiting (<= ${READY_WAIT}s) for ${POD} to return to Ready"
kubectl --context "$K8S_CONTEXT" -n "$NAMESPACE" wait --for=condition=Ready \
  "pod/${POD}" --timeout="${READY_WAIT}s" >/dev/null \
  || die "${POD} did not return to Ready within ${READY_WAIT}s"
ok "RECOVERED: ${POD} is Ready again (restartCount=${NOW})"

# --- 6. confirm the service STILL serves after the crash --------------------
RESP2="$(predict -H "X-API-Key: ${KEY}")"
[[ "$RESP2" == *"\"model_version\":\"${STABLE_VERSION}\""* ]] \
  || die "post-crash predict did not serve model_version=${STABLE_VERSION}: ${RESP2}"
ok "post-crash: service still serves model_version=${STABLE_VERSION}"

# --- 7. alert state (informational) -----------------------------------------
log "alert state (informational — a transient self-healed crash fires no sustained alert)"
report_alerts

# --- 8. final stability check -----------------------------------------------
verify_stable || die "cluster not stable after crash scenario"

ok "SCENARIO PASS: crash_model_server — pod ${POD} crashed, kubelet restarted it (RESTARTS ${BEFORE}→${NOW}), recovered Ready, service kept serving model_version=${STABLE_VERSION}"
printf 'RECOVERY VERIFIED: restartCount %s→%s on the SAME pod %s, pod Ready, /v1/predict serves %s, base ingress + HPA stable.\n' \
  "$BEFORE" "$NOW" "$POD" "$STABLE_VERSION"
