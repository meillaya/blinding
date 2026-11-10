"""Pinned model manifest.

Every download names a repository, a full commit hash, and the SHA-256 and size
of each shard. A manifest that says `main` or omits a hash is rejected before
anything touches the network.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blinding.errors import ManifestError

REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
QUANT_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class ModelFile:
    """One GGUF file, or one shard of a split model."""

    quant: str
    shard: int
    file: str
    sha256: str
    size: int

    def validate(self) -> None:
        if not QUANT_RE.match(self.quant):
            raise ManifestError(f"invalid quant name {self.quant!r}")
        if self.shard < 1:
            raise ManifestError(f"{self.file}: shard must be at least 1")
        if not self.file.endswith(".gguf") or "/" in self.file:
            raise ManifestError(f"invalid model filename {self.file!r}")
        if not SHA256_RE.match(self.sha256):
            raise ManifestError(f"{self.file}: sha256 must be 64 lowercase hex characters")
        if self.size <= 0:
            raise ManifestError(f"{self.file}: size must be positive")


@dataclass(frozen=True, slots=True)
class Model:
    """One model with one or more quantisations."""

    id: str
    repo: str
    revision: str
    context: int
    default_quant: str
    files: tuple[ModelFile, ...]

    def validate(self) -> None:
        if not MODEL_ID_RE.match(self.id):
            raise ManifestError(f"invalid model id {self.id!r}")
        if "/" not in self.repo or ".." in self.repo:
            raise ManifestError(f"{self.id}: repo must be owner/name")
        if not REVISION_RE.match(self.revision):
            raise ManifestError(f"{self.id}: revision must be a 40-character commit hash")
        if self.context < 256:
            raise ManifestError(f"{self.id}: context must be at least 256")
        if not self.files:
            raise ManifestError(f"{self.id}: at least one file is required")
        filenames: set[str] = set()
        for model_file in self.files:
            model_file.validate()
            if model_file.file in filenames:
                raise ManifestError(f"{self.id}: duplicate file {model_file.file}")
            filenames.add(model_file.file)
        quants = self.quants()
        if self.default_quant not in quants:
            raise ManifestError(f"{self.id}: default_quant {self.default_quant!r} is not a file")
        for quant in quants:
            shards = sorted(f.shard for f in self.files if f.quant == quant)
            if shards != list(range(1, len(shards) + 1)):
                raise ManifestError(f"{self.id}: {quant} shards are not numbered 1..n")

    def quants(self) -> tuple[str, ...]:
        """Quantisations in manifest order, without duplicates."""
        seen: list[str] = []
        for model_file in self.files:
            if model_file.quant not in seen:
                seen.append(model_file.quant)
        return tuple(seen)

    def files_for(self, quant: str) -> tuple[ModelFile, ...]:
        """Shards for `quant`, in order."""
        selected = tuple(f for f in self.files if f.quant == quant)
        if not selected:
            raise ManifestError(f"{self.id}: no files for quant {quant!r}")
        return tuple(sorted(selected, key=lambda f: f.shard))


@dataclass(frozen=True, slots=True)
class Manifest:
    """Every model the tool knows how to fetch."""

    models: tuple[Model, ...]

    def get(self, model_id: str) -> Model:
        """Look up a model by id."""
        for model in self.models:
            if model.id == model_id:
                return model
        known = ", ".join(m.id for m in self.models)
        raise ManifestError(f"unknown model {model_id!r}; known models: {known}")

    def resolve(self, spec: str) -> tuple[Model, str]:
        """Split `model[:quant]` and return the model and a valid quant."""
        model_id, separator, quant = spec.partition(":")
        model = self.get(model_id)
        if not separator:
            return model, model.default_quant
        if not quant:
            raise ManifestError(f"empty quant in {spec!r}")
        if quant not in model.quants():
            known = ", ".join(model.quants())
            raise ManifestError(f"{model.id}: unknown quant {quant!r}; known quants: {known}")
        return model, quant


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ManifestError(f"{key} must be a string")
    return value


def _require_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{key} must be an integer")
    return value


def _parse_file(raw: Any) -> ModelFile:
    if not isinstance(raw, dict):
        raise ManifestError("each model file must be a TOML table")
    table: dict[str, Any] = {str(key): value for key, value in raw.items()}
    return ModelFile(
        quant=_require_str(table, "quant"),
        shard=_require_int(table, "shard"),
        file=_require_str(table, "file"),
        sha256=_require_str(table, "sha256"),
        size=_require_int(table, "size"),
    )


def load_manifest(path: Path) -> Manifest:
    """Parse and validate a manifest before any download starts."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    try:
        parsed: Any = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ManifestError(f"manifest {path} must be a TOML table")
    table: dict[str, Any] = {str(key): value for key, value in parsed.items()}
    raw_models = table.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ManifestError("manifest needs at least one [[models]] table")

    models: list[Model] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise ManifestError("each model must be a TOML table")
        model_table: dict[str, Any] = {str(key): value for key, value in raw_model.items()}
        raw_files = model_table.get("files")
        if not isinstance(raw_files, list):
            raise ManifestError("each model needs a files list")
        model = Model(
            id=_require_str(model_table, "id"),
            repo=_require_str(model_table, "repo"),
            revision=_require_str(model_table, "revision"),
            context=_require_int(model_table, "context"),
            default_quant=_require_str(model_table, "default_quant"),
            files=tuple(_parse_file(item) for item in raw_files),
        )
        model.validate()
        models.append(model)

    ids = [model.id for model in models]
    if len(set(ids)) != len(ids):
        raise ManifestError("model ids must be unique")
    return Manifest(models=tuple(models))
