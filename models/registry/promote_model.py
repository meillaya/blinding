"""Promote a registered model version to staging or prod (todo 27).

Standalone CLI::

    uv run python models/registry/promote_model.py \\
        --model-id fraud-detector --version 1.0.0 --to staging

``--to staging`` sets ``status=staging``. ``--to prod`` REQUIRES the eval gate
to have passed (``approved_for_prod: true`` — flipped by the run_evals.sh
wiring, todo 28, never by this CLI); an unapproved version is rejected with a
non-zero exit and the registry is left untouched. Promotion always reads the
CURRENT registry state and is idempotent (promoting twice yields one state).
The printed record JSON is the written state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from registry_core import (
    ModelNotRegistered,
    PromotionRejected,
    RegistryError,
    RegistryRecord,
    entry_key,
    write_model_card,
)
from registry_store import build_store


@dataclass(frozen=True, slots=True)
class PromoteArgs:
    """Typed promote CLI arguments (parsed once at the boundary)."""

    model_id: str
    version: str
    target: str
    registry_file: Path | None
    s3_bucket: str | None
    model_card_out: Path | None
    lineage: str


def _parse_args(argv: Sequence[str] | None) -> PromoteArgs:
    parser = argparse.ArgumentParser(description="Promote a registered model version.")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--version", required=True, help="Opaque registry version.")
    parser.add_argument("--to", required=True, choices=("staging", "prod"), dest="target")
    parser.add_argument(
        "--registry-file", type=Path, default=None, help="Local registry JSON (dev default)."
    )
    parser.add_argument("--s3-bucket", default=None, help="Registry lives in S3 (prod).")
    parser.add_argument("--model-card-out", type=Path, default=None, help="Model card output path.")
    parser.add_argument("--lineage", default="", help="Free-text lineage for the model card.")
    ns = parser.parse_args(argv)
    return PromoteArgs(
        model_id=str(ns.model_id),
        version=str(ns.version),
        target=str(ns.target),
        registry_file=ns.registry_file,
        s3_bucket=str(ns.s3_bucket) if ns.s3_bucket is not None else None,
        model_card_out=ns.model_card_out,
        lineage=str(ns.lineage),
    )


def _promote(record: RegistryRecord, target: str) -> RegistryRecord:
    """Apply the promotion gate: prod requires ``approved_for_prod``."""
    if target == "staging":
        return record.with_status("staging")
    if target == "prod":
        if not record.approved_for_prod:
            raise PromotionRejected(
                record.model_id,
                record.version,
                "eval gate has not passed (approved_for_prod is false); "
                "run the eval gate to approve this version before promoting to prod",
            )
        return record.with_status("prod")
    raise RegistryError(f"unknown promotion target {target!r} (expected staging or prod)")


def _run(args: PromoteArgs) -> RegistryRecord:
    """Load the CURRENT registry, gate the transition, save, re-fill the card."""
    store = build_store(s3_bucket=args.s3_bucket, registry_file=args.registry_file)
    entries = store.load()
    key = entry_key(args.model_id, args.version)
    current = entries.get(key)
    if current is None:
        raise ModelNotRegistered(args.model_id, args.version)
    promoted = _promote(current, args.target)
    entries[key] = promoted
    store.save(entries)
    write_model_card(promoted, args.model_card_out, args.lineage)
    return promoted


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        record = _run(args)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(record.to_json_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
