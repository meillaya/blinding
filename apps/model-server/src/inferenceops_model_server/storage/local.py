"""Local filesystem storage backend for model artifacts."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from inferenceops_model_server.errors import ModelNotFound
from inferenceops_model_server.schemas import ModelInfo
from inferenceops_model_server.storage.base import (
    ARTIFACT_FILENAME,
    META_FILENAME,
    ModelStorage,
    StorageError,
    artifact_dirname,
    upload_meta_bytes,
)

MODELS_DIR_ENV_VAR: Final = "MODELS_DIR"

# local.py -> storage -> inferenceops_model_server -> src -> model-server -> apps -> repo root
_REPO_ROOT: Final = Path(__file__).resolve().parents[5]
DEFAULT_MODELS_DIR: Final = _REPO_ROOT / "models" / "artifacts"


class _ModelMeta(BaseModel):
    """The identity fields every artifact's ``model_meta.json`` must carry."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_id: str
    model_version: str


def _read_model_info(meta_path: Path) -> ModelInfo | None:
    """Parse one ``model_meta.json``; return None when unreadable or malformed."""
    try:
        meta = _ModelMeta.model_validate_json(meta_path.read_bytes())
    except (OSError, ValidationError):
        return None
    return ModelInfo(model_id=meta.model_id, version=meta.model_version)


def _resolve_default_root() -> Path:
    """Precedence: ``MODELS_DIR`` env var, else the repo's ``models/artifacts``."""
    configured = os.environ.get(MODELS_DIR_ENV_VAR, "").strip()
    return Path(configured) if configured else DEFAULT_MODELS_DIR


class LocalStorageBackend(ModelStorage):
    """Resolve model artifacts under a local root directory.

    Convention: each artifact lives in ``<root>/<dir>/`` containing
    ``model.pkl`` (joblib-pickled classifier) and ``model_meta.json`` (carrying
    ``model_id`` and ``model_version``). Resolution is metadata-driven: a
    directory matches by the identity recorded in its ``model_meta.json``, NOT
    by its name — so ``get_artifact("fraud-detector", ...)`` resolves under
    ``fraud_detector/`` with no name sanitization. A directory whose metadata
    matches but whose ``model.pkl`` is absent counts as not found, as does any
    malformed metadata (which is skipped when listing).

    Root precedence: explicit ``root`` argument > ``MODELS_DIR`` env var >
    ``<repo>/models/artifacts``.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else _resolve_default_root()

    def get_artifact(self, model_id: str, version: str) -> Path:
        """Return the ``model.pkl`` path for ``model_id``/``version``."""
        for info, directory in self._iter_artifacts():
            if info.model_id == model_id and info.version == version:
                artifact = directory / ARTIFACT_FILENAME
                if artifact.is_file():
                    return artifact
                raise ModelNotFound(model_id, version)
        raise ModelNotFound(model_id, version)

    def list_models(self) -> list[ModelInfo]:
        """List every model under the root, sorted by ``(model_id, version)``."""
        return sorted(
            (info for info, _ in self._iter_artifacts()),
            key=lambda info: (info.model_id, info.version),
        )

    def put_artifact(self, model_id: str, version: str, source: Path) -> str:
        """Copy ``source``'s artifact into ``<root>/<dirname>/``; return its path.

        The returned URI is the ``model.pkl`` path under the root; the stored
        ``model_meta.json`` (synthesized when the source carries none) makes
        the copy resolvable by ``get_artifact``/``list_models`` immediately.

        Raises:
            StorageError: when ``source`` has no ``model.pkl``, its metadata
                disagrees with ``model_id``/``version``, or the copy fails.
        """
        artifact = source / ARTIFACT_FILENAME
        if not artifact.is_file():
            raise StorageError(
                str(self._root), f"source directory {source} has no {ARTIFACT_FILENAME}"
            )
        meta_bytes = upload_meta_bytes(source, model_id, version)
        target = self._root / artifact_dirname(model_id, version)
        try:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, target / ARTIFACT_FILENAME)
            (target / META_FILENAME).write_bytes(meta_bytes)
        except OSError as exc:
            raise StorageError(
                str(self._root), f"upload of {model_id}@{version} to {target} failed: {exc}"
            ) from exc
        return str(target / ARTIFACT_FILENAME)

    def _iter_artifacts(self) -> Iterator[tuple[ModelInfo, Path]]:
        """Yield ``(info, directory)`` for each parseable artifact directory."""
        if not self._root.is_dir():
            return
        for meta_path in sorted(self._root.glob(f"*/{META_FILENAME}")):
            info = _read_model_info(meta_path)
            if info is not None:
                yield info, meta_path.parent
