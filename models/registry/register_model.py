"""Register a trained model version in the model registry (todo 27).

Standalone CLI::

    uv run python models/registry/register_model.py \\
        --model-id fraud-detector --version 1.0.0 \\
        --artifact-uri models/artifacts/fraud_detector/model.pkl \\
        --framework sklearn --metrics-file models/artifacts/fraud_detector/metrics.json

Writes the spec §M5 metadata record — ``{model_id, version, artifact_uri,
framework, metrics{accuracy,f1,roc_auc}, latency_p95_ms, approved_for_prod}``
with ``approved_for_prod: false`` — into the registry envelope (local
``registry.json`` by default; S3 with ``--s3-bucket``) and fills the model
card from ``model_card_template.md``. Re-registering the same version updates
its entry in place. The version is an opaque registry label, DECOUPLED from
git semver (prior-plan M9). The printed record JSON is the written state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from registry_core import (
    RegistryError,
    RegistryRecord,
    entry_key,
    parse_metrics_file,
    write_model_card,
)
from registry_store import build_store


@dataclass(frozen=True, slots=True)
class RegisterArgs:
    """Typed register CLI arguments (parsed once at the boundary)."""

    model_id: str
    version: str
    artifact_uri: str
    framework: str
    metrics_file: Path
    latency_p95_ms: float | None
    registry_file: Path | None
    s3_bucket: str | None
    model_card_out: Path | None
    lineage: str


def _parse_args(argv: Sequence[str] | None) -> RegisterArgs:
    parser = argparse.ArgumentParser(description="Register a model version in the registry.")
    parser.add_argument("--model-id", required=True)
    parser.add_argument(
        "--version", required=True, help="Opaque registry version (decoupled from git semver)."
    )
    parser.add_argument("--artifact-uri", required=True, help="s3://... or local artifact path.")
    parser.add_argument("--framework", required=True, help="e.g. sklearn.")
    parser.add_argument(
        "--metrics-file", required=True, type=Path, help="JSON carrying accuracy, f1, roc_auc."
    )
    parser.add_argument("--latency-p95-ms", type=float, default=None)
    parser.add_argument(
        "--registry-file", type=Path, default=None, help="Local registry JSON (dev default)."
    )
    parser.add_argument("--s3-bucket", default=None, help="Store the registry in S3 (prod).")
    parser.add_argument("--model-card-out", type=Path, default=None, help="Model card output path.")
    parser.add_argument("--lineage", default="", help="Free-text lineage for the model card.")
    ns = parser.parse_args(argv)
    return RegisterArgs(
        model_id=str(ns.model_id),
        version=str(ns.version),
        artifact_uri=str(ns.artifact_uri),
        framework=str(ns.framework),
        metrics_file=ns.metrics_file,
        latency_p95_ms=ns.latency_p95_ms,
        registry_file=ns.registry_file,
        s3_bucket=str(ns.s3_bucket) if ns.s3_bucket is not None else None,
        model_card_out=ns.model_card_out,
        lineage=str(ns.lineage),
    )


def _register(args: RegisterArgs) -> RegistryRecord:
    """Parse metrics, write the record (unapproved) and fill the model card."""
    record = RegistryRecord(
        model_id=args.model_id,
        version=args.version,
        artifact_uri=args.artifact_uri,
        framework=args.framework,
        metrics=parse_metrics_file(args.metrics_file),
        latency_p95_ms=args.latency_p95_ms,
        approved_for_prod=False,
    )
    store = build_store(s3_bucket=args.s3_bucket, registry_file=args.registry_file)
    entries = store.load()
    entries[entry_key(record.model_id, record.version)] = record
    store.save(entries)
    write_model_card(record, args.model_card_out, args.lineage)
    return record


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        record = _register(args)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(record.to_json_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
