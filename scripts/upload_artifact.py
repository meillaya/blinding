"""Upload a trained artifact directory to S3 via the storage abstraction (todo 28).

Standalone CLI invoked by ``scripts/upload_model.sh``::

    uv run python scripts/upload_artifact.py \\
        --artifact-dir models/artifacts/fraud_detector --bucket my-bucket

Uploads ``model.pkl`` + ``model_meta.json`` through
``S3StorageBackend.put_artifact`` and prints ONLY the resulting artifact URI
(``s3://<bucket>/<key>``) on stdout so the caller can capture it; diagnostics
stay on stderr. Endpoint precedence: explicit ``--endpoint-url`` >
``S3_ENDPOINT_URL`` env > default AWS endpoint; credentials resolve through
the default boto3 chain (EKS IRSA or static env keys) — the identical path
the model-server's read side uses.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from inferenceops_model_server.storage import S3StorageBackend
from inferenceops_model_server.storage.base import META_FILENAME, StorageError
from pydantic import BaseModel, ConfigDict, ValidationError


class _ArtifactMeta(BaseModel):
    """The identity fields the artifact's ``model_meta.json`` must carry."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_id: str
    model_version: str


@dataclass(frozen=True, slots=True)
class UploadArgs:
    """Typed upload CLI arguments (parsed once at the boundary)."""

    artifact_dir: Path
    bucket: str
    prefix: str
    endpoint_url: str | None


def _parse_args(argv: Sequence[str] | None) -> UploadArgs:
    parser = argparse.ArgumentParser(
        description="Upload a model artifact directory to S3 (prints the artifact URI)."
    )
    parser.add_argument(
        "--artifact-dir",
        required=True,
        type=Path,
        help="Directory holding model.pkl + model_meta.json.",
    )
    parser.add_argument("--bucket", required=True, help="Target S3 bucket.")
    parser.add_argument("--prefix", default="", help="Optional key prefix within the bucket.")
    parser.add_argument(
        "--endpoint-url",
        default=None,
        help="S3 endpoint (default: S3_ENDPOINT_URL env, then the AWS endpoint).",
    )
    ns = parser.parse_args(argv)
    return UploadArgs(
        artifact_dir=ns.artifact_dir,
        bucket=str(ns.bucket),
        prefix=str(ns.prefix),
        endpoint_url=str(ns.endpoint_url) if ns.endpoint_url is not None else None,
    )


def _read_identity(artifact_dir: Path) -> tuple[str, str]:
    """The ``(model_id, model_version)`` recorded in the artifact's metadata."""
    meta_path = artifact_dir / META_FILENAME
    try:
        raw = meta_path.read_bytes()
    except OSError as exc:
        raise StorageError(str(artifact_dir), f"cannot read {meta_path}: {exc}") from exc
    try:
        meta = _ArtifactMeta.model_validate_json(raw)
    except ValidationError as exc:
        raise StorageError(
            str(artifact_dir),
            f"{meta_path} must be JSON carrying model_id and model_version: {exc}",
        ) from exc
    return meta.model_id, meta.model_version


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        model_id, version = _read_identity(args.artifact_dir)
        backend = S3StorageBackend(
            bucket=args.bucket, prefix=args.prefix, endpoint_url=args.endpoint_url
        )
        uri = backend.put_artifact(model_id, version, args.artifact_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(uri)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
