#!/usr/bin/env bash
# ============================================================================
# traffic_spike.sh — FAILURE SCENARIO 4/4 (todo 49): traffic spike.
#
# Drives a sudden burst of /v1/predict traffic (scripts/spike_load.sh, todo 20 —
# a BOUNDED parallel-curl burst, no external load generator) and proves the HPA
# scales the model-server UP and then back DOWN:
#   1. baseline: HPA model-server at 2 replicas (min 2),
#   2. spike: the external inferenceops_rps metric (prometheus-adapter, ~30 RPS
#      per replica target) drives currentReplicas ABOVE 2,
#   3. recovery: after traffic stops, the HPA's scale-down (stabilization 60s on
#      this kind overlay) returns currentReplicas to 2,
#   4. the service still serves /v1/predict (model_version=1.0.0) afterwards.
#
# Measurement discipline (todo-38 lesson): the HPA reacts on a ~30-50s metric lag,
# so the scale-up MATERIALIZES late and the scale-down takes the stabilization
# window. We therefore poll continuously: wait for the PEAK (currentReplicas > 2)
# FIRST, then wait for recovery to 2 — never judging "recovered" right after the
# spike ends (that reads the pre-scale-up state and falsely passes).
#
# Cluster-stable-aware: pre-flight memory guard; the spike is bounded (DURATION,
# self-terminating) and the HPA is declarative (min 2) so it self-recovers — no
# manifest mutation, nothing to restore. The EXIT trap reaps the spike generator
# as a backstop (spike_load.sh also self-cleans via its own trap).
#
# Usage:  scripts/failure_scenarios/traffic_spike.sh
#         SPIKE_WORKERS=20 SPIKE_DURATION=90 scripts/failure_scenarios/traffic_spike.sh
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
# shellcheck disable=SC1091  # lib path is runtime-resolved (${HERE}); analyse the combined program with `shellcheck -x`
source "${HERE}/_lib.sh"
cd "$ROOT" || exit 1

SPIKE_WORKERS="${SPIKE_WORKERS:-16}"     # parallel curl workers
SPIKE_DURATION="${SPIKE_DURATION:-80}"   # seconds the burst lasts (bounded)
PEAK_WAIT="${PEAK_WAIT:-150}"            # seconds to wait for currentReplicas > 2
RECOVER_WAIT="${RECOVER_WAIT:-240}"      # seconds to wait for recovery to 2
SPIKE_LOG="${TMPDIR:-/tmp}/inferenceops-t49-spike.log"
SPIKE_PID=""

# cleanup (EXIT trap): reap the spike generator (it also self-cleans).
# shellcheck disable=SC2329  # invoked indirectly via `trap cleanup EXIT`
cleanup() {
  set +e
  if [[ -n "$SPIKE_PID" ]]; then
    kill -- "-${SPIKE_PID}" 2>/dev/null || kill "${SPIKE_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

require_bins kubectl curl
require_cluster
memory_guard

banner "FAILURE SCENARIO 4/4 — traffic spike → HPA scales UP then recovers"
printf '  Burst: %s workers for %ss via scripts/spike_load.sh (bounded, self-terminating).\n' \
  "$SPIKE_WORKERS" "$SPIKE_DURATION"
printf '  Expect: currentReplicas 2 -> >2 (peak) -> 2 (recovery, stabilization 60s).\n'

# --- 1. baseline: HPA at 2 + service serving --------------------------------
BASELINE="$(hpa_replicas)"
[[ "$BASELINE" =~ ^[0-9]+$ ]] || BASELINE=0
log "baseline HPA model-server currentReplicas=${BASELINE} (min 2 / max 10)"

KEY="$(gateway_api_key)"
[[ -n "$KEY" ]] || die "could not read the live gateway API key (gateway down?)"
RESP="$(predict -H "X-API-Key: ${KEY}")"
[[ "$RESP" == *"\"model_version\":\"${STABLE_VERSION}\""* ]] \
  || die "pre-spike predict did not serve model_version=${STABLE_VERSION}: ${RESP}"
ok "pre-spike: serving model_version=${STABLE_VERSION}"

# --- 2. launch the bounded spike in the background --------------------------
log "launching spike_load.sh (${SPIKE_WORKERS} workers, ${SPIKE_DURATION}s) in the background"
API_KEY="$KEY" DURATION="$SPIKE_DURATION" WORKERS="$SPIKE_WORKERS" \
  scripts/spike_load.sh >"$SPIKE_LOG" 2>&1 &
SPIKE_PID=$!

# --- 3. Phase 1: wait for the scale-up (currentReplicas > 2) ----------------
log "Phase 1: waiting (<= ${PEAK_WAIT}s) for HPA to scale ABOVE 2"
PEAK=2
WAITED=0
R="$BASELINE"
while true; do
  R="$(hpa_replicas)"
  [[ "$R" =~ ^[0-9]+$ ]] || R=2
  if (( R > PEAK )); then PEAK=$R; fi
  if (( R > 2 )); then break; fi
  if (( WAITED >= PEAK_WAIT )); then
    kill -- "-${SPIKE_PID}" 2>/dev/null || kill "${SPIKE_PID}" 2>/dev/null || true
    die "HPA did not scale above 2 within ${PEAK_WAIT}s (stuck at ${R}) — spike did not drive scale-up"
  fi
  sleep 5
  WAITED=$(( WAITED + 5 ))
done
ok "HPA scaled UP: currentReplicas=${R} (and rising) — peak so far ${PEAK}"

# --- 4. Phase 2: wait for recovery to 2 (tracking the true peak) ------------
# Polls continuously through the spike's end + the scale-down stabilization window,
# so the true peak is captured and recovery is observed (not assumed).
log "Phase 2: waiting (<= ${RECOVER_WAIT}s) for HPA to recover to 2"
WAITED=0
while true; do
  R="$(hpa_replicas)"
  [[ "$R" =~ ^[0-9]+$ ]] || R=2
  if (( R > PEAK )); then PEAK=$R; fi
  if (( R == 2 )); then break; fi
  if (( WAITED >= RECOVER_WAIT )); then
    die "HPA did not recover to 2 within ${RECOVER_WAIT}s (stuck at ${R}, peak ${PEAK})"
  fi
  sleep 5
  WAITED=$(( WAITED + 5 ))
done
ok "HPA recovered to 2 (peak observed: ${PEAK})"

# Reap the spike generator (it self-terminates at DURATION; this is a backstop).
wait "$SPIKE_PID" 2>/dev/null || true
SPIKE_PID=""

# --- 5. confirm the service still serves after the spike --------------------
RESP2="$(predict -H "X-API-Key: ${KEY}")"
[[ "$RESP2" == *"\"model_version\":\"${STABLE_VERSION}\""* ]] \
  || die "post-spike predict did not serve model_version=${STABLE_VERSION}: ${RESP2}"
ok "post-spike: service still serves model_version=${STABLE_VERSION}"

verify_stable || die "cluster not stable after traffic-spike scenario"

ok "SCENARIO PASS: traffic_spike — HPA scaled model-server 2 → ${PEAK} (peak) under the spike, then recovered to 2; service kept serving model_version=${STABLE_VERSION}"
printf 'RECOVERY VERIFIED: HPA peak %s replicas, recovered to 2, /v1/predict serves %s, base ingress stable.\n' \
  "$PEAK" "$STABLE_VERSION"
