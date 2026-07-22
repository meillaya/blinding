#!/usr/bin/env bash
# ============================================================================
# destroy_dev.sh — tear down the DEV Terraform environment (todo 25).
#
# Runs `terraform destroy` against infra/terraform/envs/dev (the env created in
# todo 24). This is DESTRUCTIVE and IRREVERSIBLE, so the script refuses to run
# without an explicit confirmation:
#
#   * interactive: you must type the word `destroy` at the prompt, AND
#   * automation:  pass --yes to bypass the prompt (CI / scripted teardown).
#
# Without --yes, if stdin is not interactive (EOF / piped) the confirmation
# cannot be given and the script ABORTS without destroying anything.
#
# Usage:
#   infra/scripts/destroy_dev.sh            # prompts; type 'destroy' to proceed
#   infra/scripts/destroy_dev.sh --yes      # unattended (automation only)
#
# Requires: terraform.
# ============================================================================
set -euo pipefail

# --- configuration ----------------------------------------------------------
ENV_DIR="infra/terraform/envs/dev"
ASSUME_YES=false

# --- locate repo root (script lives in <root>/infra/scripts) ----------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: infra/scripts/destroy_dev.sh [--yes]

  --yes, -y   Bypass the interactive confirmation (automation only).
  --help, -h  Show this help.

Without --yes you must type 'destroy' at the prompt to proceed.
EOF
}

# --- 0. parse args ----------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=true ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $arg" ;;
  esac
done

# --- 1. sanity --------------------------------------------------------------
command -v terraform >/dev/null 2>&1 || die "required binary not found on PATH: terraform"
[[ -d "$ENV_DIR" ]] || die "terraform env not found: $ENV_DIR"
compgen -G "${ENV_DIR}/*.tf" >/dev/null \
  || die "no *.tf files in $ENV_DIR — nothing to destroy (env from todo 24 present?)"

# --- 2. confirmation gate ---------------------------------------------------
if [[ "$ASSUME_YES" == true ]]; then
  log "--yes given: bypassing interactive confirmation (automation mode)"
else
  cat >&2 <<EOF

  !! WARNING !!  You are about to DESTROY the DEV environment.
     terraform destroy  -chdir=${ENV_DIR}
  This permanently deletes every resource Terraform manages there.

EOF
  # `read` returns non-zero on EOF (non-interactive stdin) -> answer stays
  # empty -> we abort. Destroy is NEVER reached without the typed word.
  answer=""
  read -r -p "Type 'destroy' to confirm (anything else aborts): " answer || answer=""
  if [[ "$answer" != "destroy" ]]; then
    log "confirmation not received (got: '${answer:-<eof>}') — ABORTING; nothing was destroyed"
    exit 1
  fi
  log "confirmed"
fi

# --- 3. destroy -------------------------------------------------------------
log "terraform init (${ENV_DIR})"
terraform -chdir="$ENV_DIR" init -input=false

log "terraform destroy (${ENV_DIR})"
# The confirmation gate above has already been passed (typed 'destroy' or
# --yes), so -auto-approve avoids a second redundant prompt.
terraform -chdir="$ENV_DIR" destroy -input=false -auto-approve

log "dev environment destroyed"
