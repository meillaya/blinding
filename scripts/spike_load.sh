#!/usr/bin/env bash
# spike_load.sh — lightweight SPIKE load generator (todo 20).
#
# Drives a sudden burst of /v1/predict requests at the local kind ingress to
# push inferenceops_request_count_total RPS up and trigger the custom-RPS HPA
# (and, alternatively, the KEDA ScaledObject) to scale model-server 2 -> 5+.
#
# Deliberately k6-free (W8): plain parallel `curl` loops, no external load
# generator required. Every request carries the ingress Host header and the
# gateway API key. ~1 in 12 requests is MALFORMED (wrong feature count) so the
# gateway returns 422 — this proves error traffic is counted and does NOT crash
# the scaler (adversarial: malformed_input).
#
# BOUNDED + KILLABLE: runs for $DURATION seconds then stops on its own; Ctrl-C
# (SIGINT/SIGTERM) tears down every worker immediately via the EXIT trap, so it
# never leaves a load generator running after QA.
#
# Usage:
#   scripts/spike_load.sh                 # 24 workers for 75s at localhost:8080
#   DURATION=120 WORKERS=32 scripts/spike_load.sh
#   BASE_URL=http://localhost:8080 API_KEY=test-key scripts/spike_load.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
HOST_HEADER="${HOST_HEADER:-inferenceops.local}"
API_KEY="${API_KEY:-test-key}"
DURATION="${DURATION:-75}"
WORKERS="${WORKERS:-24}"

PREDICT_URL="${BASE_URL}/v1/predict"
# 20 features — matches the fraud-detector model's expected input width.
GOOD_BODY='{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'
# 3 features — wrong width; the model-server validates and returns HTTP 422.
BAD_BODY='{"features":[0.1,0.2,0.3]}'

END_EPOCH=$(( $(date +%s) + DURATION ))

echo "spike_load: ${WORKERS} workers -> ${PREDICT_URL} (Host: ${HOST_HEADER}) for ${DURATION}s"
echo "spike_load: ~1/12 requests malformed (expect some 422s); ends at $(date -d "@${END_EPOCH}" '+%H:%M:%S' 2>/dev/null || date -r "${END_EPOCH}" '+%H:%M:%S' 2>/dev/null || echo 'soon')"

worker() {
  local id="$1" n=0
  while (( $(date +%s) < END_EPOCH )); do
    local body="$GOOD_BODY"
    # Every 12th request per worker is malformed (422) — exercises the error path.
    (( n % 12 == 11 )) && body="$BAD_BODY"
    curl -s -o /dev/null --max-time 5 \
      -X POST "$PREDICT_URL" \
      -H "Host: ${HOST_HEADER}" \
      -H 'Content-Type: application/json' \
      -H "X-API-Key: ${API_KEY}" \
      -d "$body" || true
    n=$(( n + 1 ))
  done
  echo "spike_load: worker ${id} done (${n} reqs)"
}

PIDS=()
# Tear down ALL workers on exit / Ctrl-C so nothing is left running.
cleanup() {
  (( ${#PIDS[@]} )) || return 0
  kill "${PIDS[@]}" >/dev/null 2>&1 || true
  wait "${PIDS[@]}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for (( w=0; w<WORKERS; w++ )); do
  worker "$w" &
  PIDS+=( "$!" )
done

# Wait for the wall-clock deadline; workers self-terminate at END_EPOCH.
wait "${PIDS[@]}" >/dev/null 2>&1 || true
echo "spike_load: complete — traffic stopped; HPA should scale down after the stabilization window"
