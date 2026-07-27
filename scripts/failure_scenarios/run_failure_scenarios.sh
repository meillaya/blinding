#!/usr/bin/env bash
# ============================================================================
# run_failure_scenarios.sh — failure-scenario suite runner (todo 49).
#
# Runs the FOUR executable failure scenarios in sequence against the running kind
# cluster, verifies each one's RECOVERY independently (not just trusting the
# scenario's own assertion), then reports the TWO documented-only procedures
# (GPU memory pressure + node failure — not executable on single-node CPU kind):
#
#   EXECUTABLE (run + recovery-verified):
#     1. crash_model_server.sh   crash a pod → kubelet restart (RESTARTS++) + serves
#     2. bad_model_deploy.sh     degraded model → eval gate BLOCKS (REGRESSION DETECTED)
#     3. latency_regression.sh   canary p95 regression → canary_check.sh AUTO-ROLLBACK
#     4. traffic_spike.sh        burst → HPA scales UP (>2) then recovers to 2
#
#   DOCUMENTED (reported, not run — clearly marked NOT EXECUTABLE HERE):
#     - gpu_memory_pressure.md   (no GPU / no DCGM on the CPU-only kind node)
#     - node_failure.md          (single node = the whole cluster; nothing to reschedule to)
#
# Each scenario is self-contained + self-restoring; this runner ALSO calls the
# idempotent scripts/rollback.sh between scenarios (backstop) and runs an
# independent verify_stable after each, so a scenario that lies about its cleanup
# is still caught. It does NOT abort on the first failure — it runs all four and
# reports a per-scenario PASS/FAIL plus the documented count.
#
# QA records:
#   /tmp/qa/t49_failures.txt         summary: 4 executable PASS + 2 documented
#   /tmp/qa/t49_failures_detail.txt  per-scenario recovery verification
#   /tmp/qa/t49_logs/<name>.log      full output of each scenario
#
# Usage:  scripts/failure_scenarios/run_failure_scenarios.sh
# ============================================================================
set -uo pipefail   # no -e: run ALL scenarios and report, don't abort on first failure

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib.sh
# shellcheck disable=SC1091  # lib path is runtime-resolved (${HERE}); analyse the combined program with `shellcheck -x`
source "${HERE}/_lib.sh"
cd "$ROOT" || exit 1

SCENARIOS=(crash_model_server bad_model_deploy latency_regression traffic_spike)
DOCUMENTED=(gpu_memory_pressure node_failure)

QA_DIR="/tmp/qa"
QA_SUMMARY="${QA_DIR}/t49_failures.txt"
QA_DETAIL="${QA_DIR}/t49_failures_detail.txt"
QA_LOG_DIR="${QA_DIR}/t49_logs"

require_bins kubectl curl python3
require_cluster
memory_guard
mkdir -p "$QA_DIR" "$QA_LOG_DIR"

TS="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
{
  echo "todo 49 — failure-scenario suite (per-scenario recovery verification)"
  echo "timestamp: ${TS}  cluster: ${K8S_CONTEXT}  namespace: ${NAMESPACE}"
  echo
} > "$QA_DETAIL"

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

# --- run the executable scenarios -------------------------------------------
for name in "${SCENARIOS[@]}"; do
  banner "RUNNING EXECUTABLE SCENARIO: ${name}"
  LOG="${QA_LOG_DIR}/${name}.log"
  SCRIPT="scripts/failure_scenarios/${name}.sh"

  RC=0
  "$SCRIPT" 2>&1 | tee "$LOG"
  RC=${PIPESTATUS[0]}

  # The scenario's own evidence lines (emitted on success).
  PASS_LINE="$(grep -m1 'SCENARIO PASS:' "$LOG" 2>/dev/null || echo '(no SCENARIO PASS line emitted)')"
  RECOVERY_LINE="$(grep -m1 '^RECOVERY VERIFIED:' "$LOG" 2>/dev/null || echo '(no RECOVERY VERIFIED line emitted)')"

  # INDEPENDENT post-scenario stability check (do not trust the scenario alone).
  STABLE_RC=0
  STABLE_OUT="$(verify_stable 2>&1)" || STABLE_RC=$?

  if [[ "$RC" -eq 0 && "$STABLE_RC" -eq 0 ]]; then
    VERDICT="PASS"
    PASS_COUNT=$(( PASS_COUNT + 1 ))
  else
    VERDICT="FAIL"
    FAIL_COUNT=$(( FAIL_COUNT + 1 ))
  fi
  RESULTS+=("${name}: ${VERDICT}")
  ok "scenario ${name}: ${VERDICT} (scenario rc=${RC}, independent stability rc=${STABLE_RC})"

  {
    echo "=== ${name}: ${VERDICT} (scenario rc=${RC}, independent stability rc=${STABLE_RC}) ==="
    echo "scenario result : ${PASS_LINE}"
    echo "scenario recovery: ${RECOVERY_LINE}"
    echo "independent post-scenario stability check:"
    echo "${STABLE_OUT}"
    echo "full log: ${LOG}"
    echo
  } >> "$QA_DETAIL"

  # Backstop: leave the cluster at base before the next scenario.
  restore_base
done

# --- report the documented-only procedures ----------------------------------
DOCUMENTED_COUNT=0
{
  echo "=== documented-only procedures (NOT executable on single-node CPU kind) ==="
} >> "$QA_DETAIL"
DOC_RESULTS=()
for doc in "${DOCUMENTED[@]}"; do
  DOC_PATH="scripts/failure_scenarios/${doc}.md"
  if [[ -f "$DOC_PATH" ]] && grep -q 'NOT EXECUTABLE HERE' "$DOC_PATH"; then
    DOCUMENTED_COUNT=$(( DOCUMENTED_COUNT + 1 ))
    DOC_RESULTS+=("${doc}.md: DOCUMENTED (present + marked not-executable-here)")
    echo "${doc}.md: DOCUMENTED — present and marked 'NOT EXECUTABLE HERE'" >> "$QA_DETAIL"
  else
    DOC_RESULTS+=("${doc}.md: MISSING/UNMARKED")
    echo "${doc}.md: MISSING or not marked 'NOT EXECUTABLE HERE'" >> "$QA_DETAIL"
  fi
done
echo >> "$QA_DETAIL"

# --- final cluster stability ------------------------------------------------
FINAL_RC=0
FINAL_OUT="$(verify_stable 2>&1)" || FINAL_RC=$?
{
  echo "=== final cluster stability (after all scenarios) ==="
  echo "${FINAL_OUT}"
} >> "$QA_DETAIL"

# --- summary ----------------------------------------------------------------
if [[ "$FINAL_RC" -eq 0 ]]; then FINAL_STATE="STABLE"; else FINAL_STATE="UNSTABLE"; fi
{
  echo "todo 49 — failure-scenario suite SUMMARY"
  echo "timestamp: ${TS}  cluster: ${K8S_CONTEXT}"
  echo
  echo "executable scenarios: ${PASS_COUNT} PASS / ${FAIL_COUNT} FAIL (of ${#SCENARIOS[@]})"
  for r in "${RESULTS[@]}"; do echo "  ${r}"; done
  echo
  echo "documented procedures: ${DOCUMENTED_COUNT}/${#DOCUMENTED[@]} (not-executable-here)"
  for r in "${DOC_RESULTS[@]}"; do echo "  ${r}"; done
  echo
  echo "final cluster stability: ${FINAL_STATE}"
  echo
  echo "detail: ${QA_DETAIL}"
  echo "logs:   ${QA_LOG_DIR}/"
} > "$QA_SUMMARY"

cat "$QA_SUMMARY"

if [[ "$FAIL_COUNT" -eq 0 && "$DOCUMENTED_COUNT" -eq "${#DOCUMENTED[@]}" && "$FINAL_RC" -eq 0 ]]; then
  ok "ALL PASS: ${#SCENARIOS[@]} executable scenarios PASS + ${DOCUMENTED_COUNT} documented procedures + cluster ${FINAL_STATE}"
  exit 0
fi
err "FAILURES: ${FAIL_COUNT} scenario(s) failed, documented ${DOCUMENTED_COUNT}/${#DOCUMENTED[@]}, final cluster ${FINAL_STATE}"
exit 1
