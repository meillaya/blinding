#!/usr/bin/env bash
# ============================================================================
# upload_model.sh — upload a trained artifact to S3 via the storage abstraction
# and register it with the resulting s3:// artifact_uri (todo 28).
#
# The upload goes through S3StorageBackend.put_artifact (scripts/upload_artifact.py)
# — the SAME endpoint_url injection (explicit > S3_ENDPOINT_URL env > default
# AWS endpoint) and default boto3 credential chain (EKS IRSA / static env keys)
# as the model-server's read path — so it works against moto/localstack locally
# (set S3_ENDPOINT_URL) and real S3 in CI/prod (OIDC role or static keys).
# register_model.py then records the version with artifact_uri=s3://<bucket>/<key>
# and approved_for_prod=false; the eval gate flips approval, promote consumes it.
#
# Idempotent: re-running overwrites the same S3 objects and updates the
# registry entry in place.
#
# Usage:
#   MODEL_BUCKET=my-bucket S3_ENDPOINT_URL=http://127.0.0.1:5000 scripts/upload_model.sh
#   MODEL_BUCKET=my-bucket scripts/upload_model.sh          # real S3
#
# Env knobs (all optional except MODEL_BUCKET):
#   MODEL_BUCKET     target S3 bucket (REQUIRED — there is no default backend)
#   ARTIFACT_DIR     artifact directory (default: models/artifacts/fraud_detector)
#   S3_PREFIX        key prefix within the bucket (default: bucket root)
#   S3_ENDPOINT_URL  S3 endpoint for moto/localstack (default: real AWS)
#   METRICS_FILE     metrics JSON for registration (default: <ARTIFACT_DIR>/metrics.json)
#   FRAMEWORK        framework label for registration (default: sklearn)
#   REGISTRY_FILE    local registry JSON path (default: models/registry/registry.json)
#   MODEL_CARD_OUT   model card output path (default: models/registry/cards/)
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The model-server package is a uv workspace member with package=false (run from
# source, never installed), so point PYTHONPATH at its src root — exactly like
# the Makefile's run-local target and the pytest conftest do.
export PYTHONPATH="${ROOT}/apps/model-server/src${PYTHONPATH:+:${PYTHONPATH}}"

log() { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# json_field <file> <key> — read a top-level string field from a JSON object.
# Uses python3 (always present in this repo) so we do not depend on jq here.
json_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

ARTIFACT_DIR="${ARTIFACT_DIR:-models/artifacts/fraud_detector}"
MODEL_BUCKET="${MODEL_BUCKET:-}"
S3_PREFIX="${S3_PREFIX:-}"
METRICS_FILE="${METRICS_FILE:-${ARTIFACT_DIR}/metrics.json}"
FRAMEWORK="${FRAMEWORK:-sklearn}"
REGISTRY_FILE="${REGISTRY_FILE:-}"
MODEL_CARD_OUT="${MODEL_CARD_OUT:-}"

# --- preflight: a clear typed failure when no backend is configured ----------
[[ -n "$MODEL_BUCKET" ]] || die "MODEL_BUCKET is not set: no storage backend configured. Set MODEL_BUCKET (plus S3_ENDPOINT_URL for moto/localstack, or AWS credentials for real S3)."
[[ -f "${ARTIFACT_DIR}/model.pkl" ]] || die "artifact missing: ${ARTIFACT_DIR}/model.pkl — run 'make train' first"
[[ -f "${ARTIFACT_DIR}/model_meta.json" ]] || die "artifact metadata missing: ${ARTIFACT_DIR}/model_meta.json"
[[ -f "$METRICS_FILE" ]] || die "metrics file missing: ${METRICS_FILE}"

MODEL_ID="$(json_field "${ARTIFACT_DIR}/model_meta.json" model_id)"
MODEL_VERSION="$(json_field "${ARTIFACT_DIR}/model_meta.json" model_version)"

# --- 1. upload via the storage abstraction -----------------------------------
log "uploading ${ARTIFACT_DIR} (${MODEL_ID}@${MODEL_VERSION}) to s3://${MODEL_BUCKET}"
upload_args=(scripts/upload_artifact.py --artifact-dir "$ARTIFACT_DIR" --bucket "$MODEL_BUCKET")
[[ -n "$S3_PREFIX" ]] && upload_args+=(--prefix "$S3_PREFIX")
ARTIFACT_URI="$(uv run python "${upload_args[@]}")" || die "upload to s3://${MODEL_BUCKET} failed"
log "uploaded: ${ARTIFACT_URI}"

# --- 2. register with the s3:// artifact_uri ---------------------------------
log "registering ${MODEL_ID}@${MODEL_VERSION} (artifact_uri=${ARTIFACT_URI})"
register_args=(
  models/registry/register_model.py
  --model-id "$MODEL_ID"
  --version "$MODEL_VERSION"
  --artifact-uri "$ARTIFACT_URI"
  --framework "$FRAMEWORK"
  --metrics-file "$METRICS_FILE"
  --lineage "Uploaded to ${ARTIFACT_URI} via scripts/upload_model.sh (todo 28)."
)
[[ -n "$REGISTRY_FILE" ]] && register_args+=(--registry-file "$REGISTRY_FILE")
[[ -n "$MODEL_CARD_OUT" ]] && register_args+=(--model-card-out "$MODEL_CARD_OUT")
uv run python "${register_args[@]}"
log "registered: ${MODEL_ID}@${MODEL_VERSION} -> ${ARTIFACT_URI}"
