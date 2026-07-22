#!/usr/bin/env bash
# ============================================================================
# verify_k8s.sh — InferenceOps Kubernetes ML-workload hardening assertions
# (todo 21).
#
# Verifies, against the LIVE deployed cluster (no cached state — every check
# is a fresh `kubectl get -o json`), that the `model-server` Deployment carries
# each hardening field the manifests declare, and that the PDB / HPA /
# ServiceMonitor exist:
#
#   Deployment (k8s/base/deployment-model-server.yaml + overlays):
#     - readinessProbe  -> GET /readyz  (503 until the model loads)
#     - livenessProbe   -> GET /healthz
#     - resources       -> requests AND limits (cpu + memory)
#     - rollingUpdate   -> maxUnavailable: 0, maxSurge: 1
#     - lifecycle       -> preStop hook (SIGTERM drain)
#     - terminationGracePeriodSeconds >= 30
#     - podAntiAffinity (preferred OR required, hostname topology)
#     - imagePullPolicy in {IfNotPresent, Never}
#   Cluster resources:
#     - PodDisruptionBudget model-server  -> minAvailable >= 1
#     - HPA model-server                  -> cpu + memory + an External metric
#     - ServiceMonitor model-server       -> exists
#
# Each check prints `PASS: <name>` or `FAIL: <name> — <reason>`. Exit status:
# 0 iff every check passed, 1 otherwise. Missing resources/fields FAIL with a
# clear reason rather than crashing (jq boolean filters + guarded fetches).
#
# Usage:
#   scripts/verify_k8s.sh
#   KUBE_CONTEXT=kind-inferenceops NAMESPACE=inferenceops scripts/verify_k8s.sh
#
# Requires: kubectl (configured for the target context), jq.
# ============================================================================
# Deliberately NOT `set -e`: we accumulate every failure and report them all,
# exiting non-zero at the end. pipefail so a broken pipe in a jq pipeline is
# never mistaken for success; -u so a typo'd variable is a hard error.
set -uo pipefail

CONTEXT="${KUBE_CONTEXT:-kind-inferenceops}"
NS="${NAMESPACE:-inferenceops}"
DEPLOY="model-server"

PASS_COUNT=0
FAIL_COUNT=0

pass() { printf 'PASS: %s\n' "$1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { printf 'FAIL: %s — %s\n' "$1" "$2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# check <name> <jq-boolean-filter> <json> <fail-reason>
# PASS when the jq filter evaluates truthy against <json>; FAIL (with reason)
# when it is false/null OR jq errors (e.g. a missing intermediate field). This
# is what makes a removed field a clean FAIL instead of a cryptic crash.
check() {
  local name="$1" filter="$2" json="$3" reason="$4"
  if jq -e "$filter" >/dev/null 2>&1 <<<"$json"; then
    pass "$name"
  else
    fail "$name" "$reason"
  fi
}

# fetch <kind> <name> -> stdout JSON, or "" (and a FAIL) on any kubectl error.
# The FAIL line goes to STDERR so it is never captured into the JSON variable
# by the `VAR=$(fetch ...)` caller (stdout is reserved for the JSON payload).
fetch() {
  local kind="$1" name="$2" out rc
  out=$(kubectl --context "$CONTEXT" -n "$NS" get "$kind" "$name" -o json 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'FAIL: %s/%s exists — kubectl get %s/%s failed: %s\n' \
      "$kind" "$name" "$kind" "$name" "$(head -n1 <<<"$out")" >&2
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf ''
    return 1
  fi
  printf '%s' "$out"
}

echo "== InferenceOps k8s hardening verification =="
echo "   context=$CONTEXT namespace=$NS deployment=$DEPLOY"
echo

for dep in kubectl jq; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    echo "FAIL: prerequisite — '$dep' not found on PATH" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Deployment checks (live state of deployment/$DEPLOY, container[0])
# ---------------------------------------------------------------------------
C='.spec.template.spec.containers[0]'

DEPLOY_JSON=""
if DEPLOY_JSON=$(fetch deployment "$DEPLOY"); then
  check "readinessProbe is httpGet /readyz" \
    "$C.readinessProbe.httpGet.path == \"/readyz\"" \
    "$DEPLOY_JSON" \
    "containers[0].readinessProbe.httpGet.path is not '/readyz' (or missing)"

  check "livenessProbe is httpGet /healthz" \
    "$C.livenessProbe.httpGet.path == \"/healthz\"" \
    "$DEPLOY_JSON" \
    "containers[0].livenessProbe.httpGet.path is not '/healthz' (or missing)"

  check "resources.requests set (cpu + memory)" \
    "(($C.resources.requests.cpu // \"\") | tostring != \"\") and (($C.resources.requests.memory // \"\") | tostring != \"\")" \
    "$DEPLOY_JSON" \
    "containers[0].resources.requests is missing cpu and/or memory"

  check "resources.limits set (cpu + memory)" \
    "(($C.resources.limits.cpu // \"\") | tostring != \"\") and (($C.resources.limits.memory // \"\") | tostring != \"\")" \
    "$DEPLOY_JSON" \
    "containers[0].resources.limits is missing cpu and/or memory"

  check "strategy.rollingUpdate.maxUnavailable == 0" \
    ".spec.strategy.rollingUpdate.maxUnavailable == 0" \
    "$DEPLOY_JSON" \
    "spec.strategy.rollingUpdate.maxUnavailable is not 0 (or missing)"

  check "strategy.rollingUpdate.maxSurge == 1" \
    ".spec.strategy.rollingUpdate.maxSurge == 1" \
    "$DEPLOY_JSON" \
    "spec.strategy.rollingUpdate.maxSurge is not 1 (or missing)"

  check "lifecycle.preStop hook present" \
    "$C.lifecycle | has(\"preStop\")" \
    "$DEPLOY_JSON" \
    "containers[0].lifecycle.preStop is missing (graceful drain broken)"

  check "terminationGracePeriodSeconds >= 30" \
    "(.spec.template.spec.terminationGracePeriodSeconds // 0) >= 30" \
    "$DEPLOY_JSON" \
    "spec.template.spec.terminationGracePeriodSeconds is < 30 (or missing)"

  check "podAntiAffinity present (preferred or required)" \
    "((.spec.template.spec.affinity.podAntiAffinity // {}) | has(\"preferredDuringSchedulingIgnoredDuringExecution\") or has(\"requiredDuringSchedulingIgnoredDuringExecution\"))" \
    "$DEPLOY_JSON" \
    "spec.template.spec.affinity.podAntiAffinity has neither preferred nor required rules"

  check "imagePullPolicy in {IfNotPresent, Never}" \
    "($C.imagePullPolicy as \$p | (\$p == \"IfNotPresent\" or \$p == \"Never\"))" \
    "$DEPLOY_JSON" \
    "containers[0].imagePullPolicy is not IfNotPresent or Never (got: $(jq -r "$C.imagePullPolicy // \"<unset>\"" <<<"$DEPLOY_JSON" 2>/dev/null))"
else
  # Deployment fetch already recorded a FAIL; the checks above were skipped.
  echo "note: skipping deployment field checks (deployment/$DEPLOY unreadable)"
fi

# ---------------------------------------------------------------------------
# PodDisruptionBudget: minAvailable >= 1 (number, or a "N%" string)
# ---------------------------------------------------------------------------
PDB_JSON=""
if PDB_JSON=$(fetch pdb "$DEPLOY"); then
  check "PDB minAvailable >= 1" \
    "(.spec.minAvailable) as \$m | if (\$m | type) == \"number\" then \$m >= 1 elif (\$m | type) == \"string\" then ((\$m | rtrimstr(\"%\") | tonumber) >= 1) else false end" \
    "$PDB_JSON" \
    "pdb/$DEPLOY spec.minAvailable is < 1 (or missing/not a number)"
fi

# ---------------------------------------------------------------------------
# HPA: cpu + memory Resource metrics AND an External metric
# ---------------------------------------------------------------------------
HPA_JSON=""
if HPA_JSON=$(fetch hpa "$DEPLOY"); then
  check "HPA has cpu Resource metric" \
    "[.spec.metrics[]? | select(.type == \"Resource\" and .resource.name == \"cpu\")] | length > 0" \
    "$HPA_JSON" \
    "hpa/$DEPLOY has no Resource/cpu metric"

  check "HPA has memory Resource metric" \
    "[.spec.metrics[]? | select(.type == \"Resource\" and .resource.name == \"memory\")] | length > 0" \
    "$HPA_JSON" \
    "hpa/$DEPLOY has no Resource/memory metric"

  EXT_NAME=$(jq -r '[.spec.metrics[]? | select(.type == "External") | .external.metric.name] | first // "<none>"' <<<"$HPA_JSON")
  check "HPA has an External metric (got: $EXT_NAME)" \
    "[.spec.metrics[]? | select(.type == \"External\")] | length > 0" \
    "$HPA_JSON" \
    "hpa/$DEPLOY has no External metric (custom-RPS scaling absent)"
fi

# ---------------------------------------------------------------------------
# ServiceMonitor (kube-prometheus-stack scrape config for /metrics)
# ---------------------------------------------------------------------------
SM_JSON=""
if SM_JSON=$(fetch servicemonitor "$DEPLOY"); then
  check "ServiceMonitor scrapes a metrics endpoint" \
    "(.spec.endpoints // []) | length > 0" \
    "$SM_JSON" \
    "servicemonitor/$DEPLOY has no spec.endpoints"
fi

# ---------------------------------------------------------------------------
echo
echo "== result: $PASS_COUNT passed, $FAIL_COUNT failed =="
if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
