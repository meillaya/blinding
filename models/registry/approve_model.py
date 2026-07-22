"""Approve a registered model version for prod after the eval gate (todo 28).

Standalone CLI::

    uv run python models/registry/approve_model.py \\
        --model-id fraud-detector --version 1.0.0

Flips ``approved_for_prod`` to true for one registered version — the ONLY
mechanism that sets it (``scripts/run_evals.sh`` calls this after the three
eval gates pass; register writes false and ``promote_model.py --to prod``
refuses a version still false). Approving an unregistered version fails with
a typed ``ModelNotRegistered`` error and leaves the registry untouched.
Approval reads the CURRENT registry state, is idempotent (approving twice
yields one state), preserves every other field (``status`` included), and
re-fills the model card. The printed record JSON is the written state.
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
    RegistryError,
    RegistryRecord,
    entry_key,
    write_model_card,
)
from registry_store import build_store


@dataclass(frozen=True, slots=True)
class ApproveArgs:
    """Typed approve CLI arguments (parsed once at the boundary)."""

    model_id: str
    version: str
    registry_file: Path | None
    s3_bucket: str | None
    model_card_out: Path | None
    lineage: str


def _parse_args(argv: Sequence[str] | None) -> ApproveArgs:
    parser = argparse.ArgumentParser(
        description="Approve a registered model version for prod (eval-gate wiring)."
    )
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--version", required=True, help="Opaque registry version.")
    parser.add_argument(
        "--registry-file", type=Path, default=None, help="Local registry JSON (dev default)."
    )
    parser.add_argument("--s3-bucket", default=None, help="Registry lives in S3 (prod).")
    parser.add_argument("--model-card-out", type=Path, default=None, help="Model card output path.")
    parser.add_argument("--lineage", default="", help="Free-text lineage for the model card.")
    ns = parser.parse_args(argv)
    return ApproveArgs(
        model_id=str(ns.model_id),
        version=str(ns.version),
        registry_file=ns.registry_file,
        s3_bucket=str(ns.s3_bucket) if ns.s3_bucket is not None else None,
        model_card_out=ns.model_card_out,
        lineage=str(ns.lineage),
    )


def _run(args: ApproveArgs) -> RegistryRecord:
    """Load the CURRENT registry, flip the approval, save, re-fill the card."""
    store = build_store(s3_bucket=args.s3_bucket, registry_file=args.registry_file)
    entries = store.load()
    key = entry_key(args.model_id, args.version)
    current = entries.get(key)
    if current is None:
        raise ModelNotRegistered(args.model_id, args.version)
    approved = current.with_approved_for_prod()
    entries[key] = approved
    store.save(entries)
    write_model_card(approved, args.model_card_out, args.lineage)
    return approved


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
