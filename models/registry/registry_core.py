"""Model registry schema, typed errors, and model-card rendering (todo 27).

The registry is the MLOps lifecycle core: ``register_model.py`` writes one
spec §M5 metadata record per ``(model_id, version)`` and ``promote_model.py``
moves a record through ``staging`` → ``prod`` behind the eval gate
(``approved_for_prod``). Metadata is a JSON envelope keyed by
``"<model_id>@<version>"`` — a flat map so ``jq`` can select the prod entry
directly — stored via ``registry_store`` (local ``registry.json`` for dev,
S3 for prod).

Model versioning is DECOUPLED from git semver (prior-plan M9): ``version`` is
an opaque registry label assigned at registration, independent of any git tag,
so non-semver strings register and promote exactly like semver ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

# --- typed errors --------------------------------------------------------------


class RegistryError(Exception):
    """Raised when a registry operation fails (I/O, corrupt JSON, bad metrics)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ModelNotRegistered(RegistryError):
    """Raised when promoting a ``(model_id, version)`` absent from the registry."""

    def __init__(self, model_id: str, version: str) -> None:
        self.model_id = model_id
        self.version = version
        super().__init__(f"model {model_id!r} version {version!r} is not registered")


class PromotionRejected(RegistryError):
    """Raised when the promotion gate refuses a transition (e.g. prod unapproved)."""

    def __init__(self, model_id: str, version: str, reason: str) -> None:
        self.model_id = model_id
        self.version = version
        super().__init__(
            f"promotion of model {model_id!r} version {version!r} rejected: {reason}"
        )


# --- schema (spec §M5) -----------------------------------------------------------

PromotionTarget = Literal["staging", "prod"]


class EvalMetrics(BaseModel):
    """The eval-gate metrics recorded at registration (spec §M5 ``metrics``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accuracy: float
    f1: float
    roc_auc: float


class RegistryRecord(BaseModel):
    """One registered model version's metadata (spec §M5).

    Serialized field order IS the spec schema order. ``status`` is absent until
    ``promote_model`` sets it, so a freshly registered record carries exactly
    the seven spec fields. ``approved_for_prod`` starts false and is flipped
    only by the eval gate (run_evals.sh wiring, todo 28) — never by register.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    version: str
    artifact_uri: str
    framework: str
    metrics: EvalMetrics
    latency_p95_ms: float | None
    approved_for_prod: bool
    status: PromotionTarget | None = None

    def to_json_dict(self) -> dict[str, object]:
        """The exact spec §M5 JSON shape; ``status`` appears only once promoted."""
        payload: dict[str, object] = {
            "model_id": self.model_id,
            "version": self.version,
            "artifact_uri": self.artifact_uri,
            "framework": self.framework,
            "metrics": {
                "accuracy": self.metrics.accuracy,
                "f1": self.metrics.f1,
                "roc_auc": self.metrics.roc_auc,
            },
            "latency_p95_ms": self.latency_p95_ms,
            "approved_for_prod": self.approved_for_prod,
        }
        if self.status is not None:
            payload["status"] = self.status
        return payload

    def with_status(self, target: PromotionTarget) -> Self:
        """A copy of this record promoted to ``target`` (all else unchanged)."""
        return self.model_copy(update={"status": target})

    def with_approved_for_prod(self) -> Self:
        """A copy of this record approved for prod (all else unchanged).

        The ONLY path to ``approved_for_prod=True``: ``approve_model.py``,
        which ``scripts/run_evals.sh`` calls after the three eval gates pass.
        Register writes False; promote consumes it as the prod gate.
        """
        return self.model_copy(update={"approved_for_prod": True})


class _MetricsFile(BaseModel):
    """A metrics JSON file: the three gate metrics; extra report fields ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    accuracy: float
    f1: float
    roc_auc: float


def parse_metrics_file(path: Path) -> EvalMetrics:
    """Parse ``--metrics-file`` JSON into the gate metrics.

    Raises:
        RegistryError: when the file is unreadable, not valid JSON, or missing
            any of accuracy/f1/roc_auc (the error names the offending file and,
            for missing fields, the field names).
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"cannot read metrics file {path}: {exc}") from exc
    try:
        parsed = _MetricsFile.model_validate_json(raw)
    except ValidationError as exc:
        raise RegistryError(
            f"metrics file {path} must be JSON carrying accuracy, f1 and roc_auc: {exc}"
        ) from exc
    return EvalMetrics(accuracy=parsed.accuracy, f1=parsed.f1, roc_auc=parsed.roc_auc)


# --- registry envelope -------------------------------------------------------------


def entry_key(model_id: str, version: str) -> str:
    """The registry envelope key for one model version: ``<model_id>@<version>``."""
    return f"{model_id}@{version}"


_REGISTRY_ADAPTER: Final = TypeAdapter(dict[str, RegistryRecord])


def parse_registry(raw: bytes) -> dict[str, RegistryRecord]:
    """Parse the whole registry envelope.

    Raises:
        RegistryError: when the payload is not a JSON object whose every value
            is a valid RegistryRecord (corrupt or partially-written registry).
    """
    try:
        return _REGISTRY_ADAPTER.validate_json(raw)
    except ValidationError as exc:
        raise RegistryError(f"registry JSON is corrupt or invalid: {exc}") from exc


def serialize_registry(entries: dict[str, RegistryRecord]) -> str:
    """Serialize the envelope with stable (sorted) key order for idempotent diffs."""
    payload = {key: entries[key].to_json_dict() for key in sorted(entries)}
    return json.dumps(payload, indent=2) + "\n"


# --- model card ---------------------------------------------------------------------

TEMPLATE_PATH: Final = Path(__file__).resolve().parent / "model_card_template.md"
CARDS_DIR: Final = Path(__file__).resolve().parent / "cards"
_UNREGISTERED_STATUS: Final = "registered"
_UNMEASURED_LATENCY: Final = "not measured at registration"


def default_card_path(model_id: str, version: str) -> Path:
    """Where a model version's filled card lives: ``cards/<model_id>@<version>.md``."""
    return CARDS_DIR / f"{entry_key(model_id, version)}.md"


def render_model_card(record: RegistryRecord, template: str, *, lineage: str) -> str:
    """Fill every ``{{placeholder}}`` in the template from the record."""
    latency = (
        str(record.latency_p95_ms)
        if record.latency_p95_ms is not None
        else _UNMEASURED_LATENCY
    )
    replacements = {
        "{{model_id}}": record.model_id,
        "{{version}}": record.version,
        "{{artifact_uri}}": record.artifact_uri,
        "{{framework}}": record.framework,
        "{{accuracy}}": str(record.metrics.accuracy),
        "{{f1}}": str(record.metrics.f1),
        "{{roc_auc}}": str(record.metrics.roc_auc),
        "{{latency_p95_ms}}": latency,
        "{{approved_for_prod}}": str(record.approved_for_prod).lower(),
        "{{status}}": record.status or _UNREGISTERED_STATUS,
        "{{lineage}}": lineage,
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def write_model_card(record: RegistryRecord, out: Path | None, lineage: str = "") -> None:
    """Fill the template and write the card (``out`` or the default cards dir).

    Raises:
        RegistryError: when the template or the card path is not writable.
    """
    try:
        template = TEMPLATE_PATH.read_text()
    except OSError as exc:
        raise RegistryError(f"cannot read model card template {TEMPLATE_PATH}: {exc}") from exc
    effective_lineage = lineage or f"Trained {record.framework} artifact at {record.artifact_uri}."
    card_path = out if out is not None else default_card_path(record.model_id, record.version)
    try:
        card_path.parent.mkdir(parents=True, exist_ok=True)
        card_path.write_text(render_model_card(record, template, lineage=effective_lineage))
    except OSError as exc:
        raise RegistryError(f"cannot write model card {card_path}: {exc}") from exc
