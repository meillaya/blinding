#!/usr/bin/env bash
# ============================================================================
# deploy_staging.sh — deploy InferenceOps to STAGING (todo 25).
#
# SAFETY-FIRST: this script NEVER applies by default. Applying to staging is a
# deliberate manual operator action.
#
#   (no flag)   Print the actions it WOULD take and stop. Nothing is applied.
#   --dry-run   Read-only preview: the planned actions, a `terraform plan`
#               (when the staging env is initialized), and the rendered
#               `kustomize build` of the staging overlay. Nothing is applied.
#   --apply     Actually deploy: terraform apply (staging env) + apply the
#               rendered staging overlay to the cluster + wait for rollout.
#
# The staging k8s overlay is k8s/overlays/staging (tag :staging, ns
# inferenceops); the terraform env is infra/terraform/envs/staging (todo 24).
# The kube context defaults to $KUBE_CONTEXT or `staging`.
#
# Usage:
#   scripts/deploy_staging.sh                 # show what it would do; apply nothing
#   scripts/deploy_staging.sh --dry-run       # read-only plan + rendered manifests
#   scripts/deploy_staging.sh --apply         # MANUAL: really deploy to staging
#   KUBE_CONTEXT=staging scripts/deploy_staging.sh --dry-run
#
# Requires: kubectl (dry-run/apply); terraform (only if the staging env has
# *.tf); `kustomize` OR `kubectl kustomize` for the overlay render.
# ============================================================================
set -euo pipefail

# --- configuration ----------------------------------------------------------
STAGING_ENV_DIR="infra/terraform/envs/staging"
STAGING_OVERLAY="k8s/overlays/staging"
KUBE_CONTEXT="${KUBE_CONTEXT:-staging}"

# --- locate repo root (script lives in <root>/scripts) ----------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: scripts/deploy_staging.sh [--dry-run | --apply]

  (no flag)   Print the actions this WOULD take and exit. Applies nothing.
  --dry-run   Read-only preview: terraform plan (if the env is initialized)
              + the rendered staging overlay (kustomize build). Applies nothing.
  --apply     MANUAL deploy: terraform apply + kubectl apply + rollout wait.
  --help, -h  Show this help.
EOF
}

has_tf_files() { compgen -G "$1/*.tf" >/dev/null; }

# render_overlay -> stdout YAML of the staging overlay.
render_overlay() {
  if command -v kustomize >/dev/null 2>&1; then
    kustomize build "$STAGING_OVERLAY"
  else
    kubectl kustomize "$STAGING_OVERLAY"
  fi
}

print_planned_actions() {
  cat <<EOF
Planned actions (staging):
  1. terraform: plan + apply in ${STAGING_ENV_DIR} (AWS env from todo 24)
  2. kubernetes: render ${STAGING_OVERLAY} (kustomize) and
     kubectl --context ${KUBE_CONTEXT} apply -f -   (ns inferenceops, tag :staging)
  3. kubectl --context ${KUBE_CONTEXT} -n inferenceops rollout status
     deployment/model-server deployment/gateway
EOF
}

# --- 0. parse args ----------------------------------------------------------
MODE="none"
for arg in "$@"; do
  case "$arg" in
    --dry-run)
      [[ "$MODE" == "apply" ]] && die "--dry-run and --apply are mutually exclusive"
      MODE="dry-run" ;;
    --apply)
      [[ "$MODE" == "dry-run" ]] && die "--dry-run and --apply are mutually exclusive"
      MODE="apply" ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $arg" ;;
  esac
done

# ============================================================================
case "$MODE" in
# ----------------------------------------------------------------------------
none)
  print_planned_actions
  cat <<'EOF'

This script NEVER applies by default — nothing above was executed.
  * Re-run with --dry-run to preview (read-only terraform plan + rendered manifests).
  * Re-run with --apply  to confirm and actually deploy to staging (manual).
EOF
  exit 0
  ;;

# ----------------------------------------------------------------------------
dry-run)
  log "DRY RUN (read-only) — context: ${KUBE_CONTEXT}; NOTHING will be applied"
  command -v kubectl >/dev/null 2>&1 || die "required binary not found on PATH: kubectl"
  print_planned_actions

  # 1. terraform plan preview (read-only). Only meaningful once the staging
  #    env (todo 24) exists and is initialized; otherwise we report the exact
  #    command that WOULD run. A failed preview never fails the dry run.
  if has_tf_files "$STAGING_ENV_DIR"; then
    if command -v terraform >/dev/null 2>&1; then
      log "terraform plan preview (${STAGING_ENV_DIR})"
      if ! terraform -chdir="$STAGING_ENV_DIR" plan -input=false; then
        log "WARN: terraform plan preview failed (run 'terraform -chdir=${STAGING_ENV_DIR} init' / check creds) — continuing dry run"
      fi
    else
      log "WARN: terraform not on PATH — would run: terraform -chdir=${STAGING_ENV_DIR} plan"
    fi
  else
    log "SKIP: no *.tf in ${STAGING_ENV_DIR} yet (todo 24) — would run: terraform -chdir=${STAGING_ENV_DIR} plan"
  fi

  # 2. kustomize build preview (read-only render of the staging overlay).
  log "kustomize build preview (${STAGING_OVERLAY}) — rendered resources that WOULD be applied:"
  rendered="$(render_overlay)"
  # Client-side dry-run lists kind/name WITHOUT contacting the cluster or
  # applying anything; fall back to the raw YAML if that is unavailable.
  if ! printf '%s\n' "$rendered" \
      | kubectl apply --dry-run=client --validate=false -o name -f - 2>/dev/null; then
    printf '%s\n' "$rendered"
  fi

  log "DRY RUN complete — NOTHING was applied. Re-run with --apply to deploy."
  ;;

# ----------------------------------------------------------------------------
apply)
  log "APPLY to staging — context: ${KUBE_CONTEXT}"
  command -v kubectl >/dev/null 2>&1 || die "required binary not found on PATH: kubectl"
  print_planned_actions

  # 1. terraform apply (staging AWS env). Skipped cleanly until todo 24 lands.
  if has_tf_files "$STAGING_ENV_DIR"; then
    command -v terraform >/dev/null 2>&1 || die "required binary not found on PATH: terraform"
    log "terraform init + apply (${STAGING_ENV_DIR})"
    terraform -chdir="$STAGING_ENV_DIR" init -input=false
    # Deliberately NO -auto-approve: terraform's own prompt is the final gate.
    terraform -chdir="$STAGING_ENV_DIR" apply -input=false
  else
    log "SKIP: no *.tf in ${STAGING_ENV_DIR} yet (todo 24) — applying k8s overlay only"
  fi

  # 2. apply the rendered staging overlay.
  log "applying ${STAGING_OVERLAY} to context ${KUBE_CONTEXT}"
  render_overlay | kubectl --context "$KUBE_CONTEXT" apply -f -

  # 3. wait for the app rollout.
  log "waiting (<=360s) for rollout: deployment/model-server"
  kubectl --context "$KUBE_CONTEXT" -n inferenceops rollout status deployment/model-server --timeout=360s
  log "waiting (<=300s) for rollout: deployment/gateway"
  kubectl --context "$KUBE_CONTEXT" -n inferenceops rollout status deployment/gateway --timeout=300s

  log "staging deploy complete"
  kubectl --context "$KUBE_CONTEXT" -n inferenceops get pods
  ;;
esac
