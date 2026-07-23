#!/usr/bin/env bash
# ============================================================================
# check_runbook_coverage.sh — every Prometheus alert rule must have a runbook
# entry (todo 36).
#
# Parses `alert:` names out of observability/prometheus/rules/*.yaml and
# verifies each one has a heading entry (`### <AlertName>`) in
# docs/incident-runbook.md.
#
# Exit codes:
#   0  every alert has a runbook entry
#   1  one or more alerts are missing a runbook entry (names printed to stderr)
#   2  environment error (rules dir / runbook / rule files missing, or no
#      alert names parsed) — a broken check must never pass silently
#
# Usage:
#   scripts/check_runbook_coverage.sh
#   RULES_DIR=<dir> RUNBOOK=<file> scripts/check_runbook_coverage.sh
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RULES_DIR="${RULES_DIR:-${ROOT}/observability/prometheus/rules}"
RUNBOOK="${RUNBOOK:-${ROOT}/docs/incident-runbook.md}"

[[ -d "$RULES_DIR" ]] || { printf 'ERROR: rules dir not found: %s\n' "$RULES_DIR" >&2; exit 2; }
[[ -f "$RUNBOOK" ]] || { printf 'ERROR: runbook not found: %s\n' "$RUNBOOK" >&2; exit 2; }

shopt -s nullglob
rule_files=("$RULES_DIR"/*.yaml)
shopt -u nullglob
if (( ${#rule_files[@]} == 0 )); then
  printf 'ERROR: no rule files (*.yaml) in %s\n' "$RULES_DIR" >&2
  exit 2
fi

# Alert names from lines shaped `  - alert: Name` (trailing comment/whitespace
# stripped; PrometheusRule is the only home of `alert:` keys in these files).
ALERTS="$(awk '
  /^[[:space:]]*-[[:space:]]*alert:[[:space:]]*/ {
    sub(/^[[:space:]]*-[[:space:]]*alert:[[:space:]]*/, "")
    sub(/[[:space:]].*$/, "")
    if (length($0) > 0) print
  }
' "${rule_files[@]}" | sort -u)"

if [[ -z "$ALERTS" ]]; then
  printf 'ERROR: no alert names parsed from %s/*.yaml\n' "$RULES_DIR" >&2
  exit 2
fi

total=0
missing=0
while IFS= read -r alert; do
  [[ -n "$alert" ]] || continue
  total=$((total + 1))
  if [[ ! "$alert" =~ ^[A-Za-z0-9_]+$ ]]; then
    printf 'MISSING: %s (unparseable alert name — parser bug, not a runbook entry)\n' "$alert" >&2
    missing=$((missing + 1))
    continue
  fi
  # Entry marker: a markdown heading starting with the exact alert name
  # (`### HighP95Latency` or `### HighP95Latency: <title>`). The boundary
  # class rejects prefix matches: HighP95 must not satisfy HighP95Latency.
  if grep -qE "^###+[[:space:]]+${alert}([^A-Za-z0-9_]|$)" "$RUNBOOK"; then
    printf 'COVERED: %s\n' "$alert"
  else
    # shellcheck disable=SC2016  # literal `### <name>` doc marker, not an expansion
    printf 'MISSING: %s (no `### %s` entry in %s)\n' "$alert" "$alert" "$RUNBOOK" >&2
    missing=$((missing + 1))
  fi
done <<< "$ALERTS"

if (( missing > 0 )); then
  printf '\nFAIL: %d of %d alert(s) missing a runbook entry in %s\n' \
    "$missing" "$total" "$RUNBOOK" >&2
  exit 1
fi
printf '\nOK: all %d alert(s) have a runbook entry in %s\n' "$total" "$RUNBOOK"
