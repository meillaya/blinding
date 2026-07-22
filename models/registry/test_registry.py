"""Model registry tests: register → promote → model card.

Covers the todo 27 acceptance: ``register_model`` writes the EXACT spec §M5
metadata schema (``approved_for_prod: false``); ``promote_model --to staging``
sets ``status=staging``; ``--to prod`` is REJECTED unless the eval gate passed
(``approved_for_prod: true``); model versioning is DECOUPLED from git semver
(the version is an opaque registry label, so a non-semver string registers and
promotes independently per version). Local store runs against a real tmp_path.
The S3 backend is covered in ``test_registry_s3.py`` (moto), mirroring the
repo's local/S3 storage-test split (todo 7/26).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import approve_model
import promote_model
import pytest
import register_model
from registry_core import (
    EvalMetrics,
    RegistryRecord,
    entry_key,
    parse_registry,
    serialize_registry,
)

METRICS: Final = {
    "accuracy": 0.9655,
    "f1": 0.6102,
    "roc_auc": 0.9184,
    "n_samples": 2000,  # extra keys (eval-report fields) must be ignored
    "seed": 42,
}
EXACT_SCHEMA_KEYS: Final = [
    "model_id",
    "version",
    "artifact_uri",
    "framework",
    "metrics",
    "latency_p95_ms",
    "approved_for_prod",
]


@dataclass(frozen=True, slots=True)
class Env:
    """One isolated registry environment under a pytest tmp_path."""

    registry_file: Path
    metrics_file: Path
    card_out: Path


def _record(
    *,
    model_id: str = "fraud-detector",
    version: str = "1.0.0",
    approved_for_prod: bool = False,
    status: Literal["staging", "prod"] | None = None,
) -> RegistryRecord:
    return RegistryRecord(
        model_id=model_id,
        version=version,
        artifact_uri="models/artifacts/fraud_detector/model.pkl",
        framework="sklearn",
        metrics=EvalMetrics(accuracy=0.9655, f1=0.6102, roc_auc=0.9184),
        latency_p95_ms=None,
        approved_for_prod=approved_for_prod,
        status=status,
    )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    metrics_file = tmp_path / "metrics.json"
    metrics_file.write_text(json.dumps(METRICS))
    return Env(
        registry_file=tmp_path / "registry.json",
        metrics_file=metrics_file,
        card_out=tmp_path / "card.md",
    )


def run_register(env: Env, *, version: str = "1.0.0", extra: Sequence[str] = ()) -> int:
    return register_model.main(
        [
            "--model-id",
            "fraud-detector",
            "--version",
            version,
            "--artifact-uri",
            "models/artifacts/fraud_detector/model.pkl",
            "--framework",
            "sklearn",
            "--metrics-file",
            str(env.metrics_file),
            "--registry-file",
            str(env.registry_file),
            "--model-card-out",
            str(env.card_out),
            *extra,
        ]
    )


def run_promote(env: Env, *, version: str = "1.0.0", to: str) -> int:
    return promote_model.main(
        [
            "--model-id",
            "fraud-detector",
            "--version",
            version,
            "--to",
            to,
            "--registry-file",
            str(env.registry_file),
            "--model-card-out",
            str(env.card_out),
        ]
    )


def run_approve(env: Env, *, version: str = "1.0.0") -> int:
    return approve_model.main(
        [
            "--model-id",
            "fraud-detector",
            "--version",
            version,
            "--registry-file",
            str(env.registry_file),
            "--model-card-out",
            str(env.card_out),
        ]
    )


def read_entry(env: Env, version: str = "1.0.0") -> dict[str, object]:
    raw: dict[str, dict[str, object]] = json.loads(env.registry_file.read_text())
    return raw[entry_key("fraud-detector", version)]


# --- register -----------------------------------------------------------------


def test_register_writes_exact_schema(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_register(env) == 0

    entry = read_entry(env)
    assert list(entry.keys()) == EXACT_SCHEMA_KEYS
    assert entry["approved_for_prod"] is False
    assert entry["latency_p95_ms"] is None
    assert entry["metrics"] == {"accuracy": 0.9655, "f1": 0.6102, "roc_auc": 0.9184}
    assert "status" not in entry  # status appears only after promotion

    printed: dict[str, object] = json.loads(capsys.readouterr().out)
    assert printed == entry


def test_register_records_latency_when_given(env: Env) -> None:
    assert run_register(env, extra=["--latency-p95-ms", "12.5"]) == 0
    assert read_entry(env)["latency_p95_ms"] == 12.5


def test_reregister_updates_entry_in_place(env: Env) -> None:
    assert run_register(env) == 0
    updated = dict(METRICS, accuracy=0.99)
    env.metrics_file.write_text(json.dumps(updated))
    assert run_register(env) == 0

    registry: dict[str, object] = json.loads(env.registry_file.read_text())
    assert len(registry) == 1  # same version → one entry, updated
    assert read_entry(env)["metrics"] == {
        "accuracy": 0.99,
        "f1": 0.6102,
        "roc_auc": 0.9184,
    }


def test_register_missing_metrics_file_fails(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.metrics_file.unlink()
    assert run_register(env) == 1
    assert "metrics file" in capsys.readouterr().err
    assert not env.registry_file.exists()  # nothing written on failure


def test_register_malformed_metrics_json_fails(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.metrics_file.write_text("{not json")
    assert run_register(env) == 1
    assert "metrics file" in capsys.readouterr().err


def test_register_incomplete_metrics_fails(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.metrics_file.write_text(json.dumps({"accuracy": 0.9}))
    assert run_register(env) == 1
    err = capsys.readouterr().err
    assert "f1" in err and "roc_auc" in err  # names the missing metrics


# --- promote ------------------------------------------------------------------


def test_promote_staging_sets_status(env: Env) -> None:
    assert run_register(env) == 0
    assert run_promote(env, to="staging") == 0
    assert read_entry(env)["status"] == "staging"


def test_promote_prod_rejected_without_approval(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_register(env) == 0
    assert run_promote(env, to="prod") == 1

    err = capsys.readouterr().err
    assert "approved_for_prod" in err  # clear rejection reason
    assert "status" not in read_entry(env)  # registry left untouched


def test_promote_prod_succeeds_after_eval_gate(env: Env) -> None:
    assert run_register(env) == 0
    # Simulate the eval gate (todo 28 wiring): it flips approved_for_prod.
    registry = json.loads(env.registry_file.read_text())
    registry[entry_key("fraud-detector", "1.0.0")]["approved_for_prod"] = True
    env.registry_file.write_text(json.dumps(registry))

    assert run_promote(env, to="prod") == 0
    entry = read_entry(env)
    assert entry["status"] == "prod"
    assert entry["approved_for_prod"] is True


def test_promote_reads_current_registry_state(env: Env) -> None:
    """Promote loads the registry fresh each run (no stale in-memory state)."""
    assert run_register(env) == 0
    assert run_promote(env, to="staging") == 0
    # A second process's registration lands between promotions...
    assert run_register(env, version="2.0.0") == 0
    # ...and the first version's promotion state survives intact.
    assert run_promote(env, to="prod") == 1  # still unapproved
    assert read_entry(env)["status"] == "staging"


def test_promote_twice_is_idempotent(env: Env) -> None:
    assert run_register(env) == 0
    assert run_promote(env, to="staging") == 0
    first = env.registry_file.read_text()
    assert run_promote(env, to="staging") == 0
    assert env.registry_file.read_text() == first


def test_promote_unregistered_model_fails(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_promote(env, to="staging") == 1
    assert "not registered" in capsys.readouterr().err


def test_promote_corrupt_registry_fails(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.registry_file.write_text("{garbage")
    assert run_promote(env, to="staging") == 1
    assert "corrupt" in capsys.readouterr().err


# --- approve (eval-gate wiring, todo 28) ---------------------------------------


def test_approve_flips_approved_for_prod(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_register(env) == 0
    assert read_entry(env)["approved_for_prod"] is False

    capsys.readouterr()  # discard register's printed record; assert approve's below
    assert run_approve(env) == 0

    entry = read_entry(env)
    assert entry["approved_for_prod"] is True
    assert list(entry.keys()) == EXACT_SCHEMA_KEYS  # no field added or dropped
    printed: dict[str, object] = json.loads(capsys.readouterr().out)
    assert printed == entry


def test_approve_is_idempotent(env: Env) -> None:
    assert run_register(env) == 0
    assert run_approve(env) == 0
    first = env.registry_file.read_text()
    assert run_approve(env) == 0
    assert env.registry_file.read_text() == first


def test_approve_unregistered_model_fails(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_approve(env) == 1
    assert "not registered" in capsys.readouterr().err
    assert not env.registry_file.exists()  # nothing written on failure


def test_approve_preserves_status_and_metrics(env: Env) -> None:
    assert run_register(env) == 0
    assert run_promote(env, to="staging") == 0
    assert run_approve(env) == 0

    entry = read_entry(env)
    assert entry["approved_for_prod"] is True
    assert entry["status"] == "staging"  # approval does not demote
    assert entry["metrics"] == {"accuracy": 0.9655, "f1": 0.6102, "roc_auc": 0.9184}


def test_promote_prod_succeeds_after_approve(env: Env) -> None:
    """The full wiring: register -> eval-gate approval -> promote --to prod."""
    assert run_register(env) == 0
    assert run_promote(env, to="prod") == 1  # gate closed before approval

    assert run_approve(env) == 0
    assert run_promote(env, to="prod") == 0

    entry = read_entry(env)
    assert entry["status"] == "prod"
    assert entry["approved_for_prod"] is True


def test_approve_refills_model_card(env: Env) -> None:
    assert run_register(env) == 0
    assert "false" in env.card_out.read_text()  # approved_for_prod rendered false
    assert run_approve(env) == 0
    card = env.card_out.read_text()
    assert "true" in card
    assert "{{" not in card  # every placeholder replaced


# --- decoupled versioning ------------------------------------------------------


def test_versioning_is_decoupled_from_git_semver(env: Env) -> None:
    """Registry versions are opaque labels, independent of git tags (prior M9)."""
    non_semver = "2026.07.22-rc1"  # a git semver parser would reject this
    assert run_register(env, version="1.0.0") == 0
    assert run_register(env, version=non_semver) == 0
    assert run_promote(env, version="1.0.0", to="staging") == 0

    assert read_entry(env, version="1.0.0")["status"] == "staging"
    assert "status" not in read_entry(env, version=non_semver)  # v2 untouched


# --- model card -----------------------------------------------------------------


def test_model_card_filled_from_template(env: Env) -> None:
    assert run_register(env) == 0
    card = env.card_out.read_text()
    assert "fraud-detector" in card
    assert "1.0.0" in card
    assert "0.9655" in card
    assert "{{" not in card  # every placeholder replaced


def test_model_card_reflects_promotion(env: Env) -> None:
    assert run_register(env) == 0
    assert "registered" in env.card_out.read_text()
    assert run_promote(env, to="staging") == 0
    assert "staging" in env.card_out.read_text()


# --- core round-trip -------------------------------------------------------------


def test_serialize_parse_round_trip_preserves_schema() -> None:
    entries = {
        entry_key("fraud-detector", "1.0.0"): _record(),
        entry_key("fraud-detector", "2.0.0"): _record(version="2.0.0", status="staging"),
    }
    reparsed = parse_registry(serialize_registry(entries).encode())
    assert reparsed == entries
    first = reparsed["fraud-detector@1.0.0"]
    assert list(first.to_json_dict().keys()) == EXACT_SCHEMA_KEYS


def test_with_approved_for_prod_changes_only_the_approval() -> None:
    record = _record(status="staging")
    approved = record.with_approved_for_prod()
    assert approved.approved_for_prod is True
    assert approved == record.model_copy(update={"approved_for_prod": True})
    assert record.approved_for_prod is False  # the source record is untouched
