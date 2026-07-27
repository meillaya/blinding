#!/usr/bin/env bash
# run_load_test.sh — parameterized k6 load-test runner (todo 37, Wave 8 / C6b).
#
# Single entrypoint for the k6 scenarios. Picks the script by SCENARIO, passes
# the target + auth via `k6 -e`, tags the run, and ALWAYS writes a machine-
# readable JSON stream to evals/reports/k6-<scenario>.json (the benchmark
# generator, todo 40, parses these). The Makefile load-test-* targets call this
# wrapper so the JSON results land on every run.
#
# Usage:
#   scripts/run_load_test.sh                    # smoke @ http://localhost:8080
#   SCENARIO=stress scripts/run_load_test.sh
#   SCENARIO=spike scripts/run_load_test.sh     # k6 spike (autoscaling trigger)
#   SCENARIO=soak DURATION=5m scripts/run_load_test.sh
#   SCENARIO=smoke TARGET=http://localhost:8080 DURATION=45s scripts/run_load_test.sh
#
# NOTE on spike: SCENARIO=spike runs the k6 spike (load_tests/k6/spike.js). This
# is DISTINCT from `make load-test-spike`, which runs todo 20's lightweight curl
# burst (scripts/spike_load.sh) — that target is preserved as the verified
# autoscaling demo. Both drive HPA scale-up; the k6 spike additionally writes the
# --out json stream the benchmark generator (todo 40) parses.
#
# k6 ships via devenv (not the default PATH): run inside `devenv shell`, or the
# wrapper exits 2 with a hint if k6 is absent.
set -euo pipefail

SCENARIO="${SCENARIO:-smoke}"
TARGET="${TARGET:-http://localhost:8080}"
HOST_HEADER="${HOST_HEADER:-inferenceops.local}"
API_KEY="${API_KEY:-dev-key-REPLACE-ME-0000000000000000}"
DURATION="${DURATION:-30s}"

# Repo root = parent of this script's dir, so the wrapper works from any CWD.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Map SCENARIO -> k6 script. Only the scenarios that EXIST are valid (smoke/
# stress from todo 37, spike/soak from todo 38). Guard against a typo'd scenario.
case "${SCENARIO}" in
  smoke | stress | spike | soak) SCRIPT="load_tests/k6/${SCENARIO}.js" ;;
  *)
    echo "run_load_test: unknown SCENARIO '${SCENARIO}' (expected: smoke|stress|spike|soak)" >&2
    exit 2
    ;;
esac

if ! command -v k6 >/dev/null 2>&1; then
  echo "run_load_test: k6 not on PATH — run inside 'devenv shell' (k6 is a devenv package)" >&2
  exit 2
fi

OUT_DIR="evals/reports"
mkdir -p "${ROOT}/${OUT_DIR}"
OUT="${OUT_DIR}/k6-${SCENARIO}.json"

echo "run_load_test: SCENARIO=${SCENARIO} TARGET=${TARGET} DURATION=${DURATION} -> ${OUT}"

# Run from the repo root so the relative script path + output path resolve.
cd "${ROOT}"
exec k6 run \
  --tag "scenario=${SCENARIO}" \
  -e TARGET="${TARGET}" \
  -e HOST_HEADER="${HOST_HEADER}" \
  -e API_KEY="${API_KEY}" \
  -e DURATION="${DURATION}" \
  --out "json=${OUT}" \
  "${SCRIPT}"
