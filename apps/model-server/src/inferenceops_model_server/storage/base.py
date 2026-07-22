"""Storage interface shared by the runtime loader and the model registry.

Besides the ``ModelStorage`` contract, this module carries the write-path
seam shared by every backend: the typed ``StorageError``, the artifact
filename convention (``model.pkl`` + identity-carrying ``model_meta.json``),
and the two ``put_artifact`` helpers (``artifact_dirname`` /
``upload_meta_bytes``) that keep the metadata-driven resolution invariant
intact on upload — the stored metadata IS the resolution key, so an upload
refuses an identity the source metadata disagrees with.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from inferenceops_model_server.schemas import ModelInfo

ARTIFACT_FILENAME: Final = "model.pkl"
META_FILENAME: Final = "model_meta.json"


class StorageError(Exception):
    """Raised when a storage operation fails (I/O, missing bucket, failed transfer).

    ``location`` names where the operation ran: the bucket for the S3 backend,
    the root directory for the local backend, the source directory for an
    upload rejected before any transfer.
    """

    def __init__(self, location: str, reason: str) -> None:
        self.location = location
        self.reason = reason
        super().__init__(f"storage error on {location!r}: {reason}")


class _UploadMeta(BaseModel):
    """The identity fields a source ``model_meta.json`` must agree on."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_id: str
    model_version: str


def artifact_dirname(model_id: str, version: str) -> str:
    """The one-level directory name an uploaded artifact lands under.

    Resolution is metadata-driven (the stored ``model_meta.json`` identity
    wins, never the directory name), so the name only needs to be unique per
    ``(model_id, version)`` and stay exactly one path level: both backends
    match ``<dirname>/model_meta.json`` one level below the root/prefix.
    Characters outside ``[A-Za-z0-9._-]`` collapse to ``_``.
    """

    def _sanitize(part: str) -> str:
        return "".join(
            ch if ch.isascii() and (ch.isalnum() or ch in "-._") else "_" for ch in part
        )

    return f"{_sanitize(model_id)}-{_sanitize(version)}"


def upload_meta_bytes(source: Path, model_id: str, version: str) -> bytes:
    """The ``model_meta.json`` payload to store alongside an uploaded artifact.

    A source carrying ``model_meta.json`` is stored verbatim (its provenance
    fields survive the upload), but its recorded identity MUST match
    ``(model_id, version)`` — the stored metadata is the resolution key, so a
    mismatch would make the artifact resolve under the wrong identity. A
    source without metadata gets a minimal synthesized one.

    Raises:
        StorageError: when the metadata is unreadable, malformed, or records
            a different ``(model_id, version)`` than the upload arguments.
    """
    meta_path = source / META_FILENAME
    if not meta_path.is_file():
        return json.dumps({"model_id": model_id, "model_version": version}).encode()
    try:
        raw = meta_path.read_bytes()
    except OSError as exc:
        raise StorageError(str(source), f"cannot read {meta_path}: {exc}") from exc
    try:
        meta = _UploadMeta.model_validate_json(raw)
    except ValidationError as exc:
        raise StorageError(
            str(source),
            f"{meta_path} must be JSON carrying model_id and model_version: {exc}",
        ) from exc
    if meta.model_id != model_id or meta.model_version != version:
        raise StorageError(
            str(source),
            f"{meta_path} records {meta.model_id}@{meta.model_version}, "
            f"refusing to upload it as {model_id}@{version}",
        )
    return raw


class ModelStorage(ABC):
    """Locate, list, and upload model artifacts.

    The single interface both the runtime model loader (Wave 2) and the model
    registry (Wave 6, which adds an S3 backend) program against. Backends
    subclass it so a forgotten method fails at construction, not at first use.
    """

    @abstractmethod
    def get_artifact(self, model_id: str, version: str) -> Path:
        """Return the path to the loadable artifact (``model.pkl``).

        Raises:
            ModelNotFound: when ``model_id``/``version`` is not registered.
        """

    @abstractmethod
    def list_models(self) -> list[ModelInfo]:
        """List metadata for every registered model, in a stable order."""

    @abstractmethod
    def put_artifact(self, model_id: str, version: str, source: Path) -> str:
        """Upload ``source`` (a directory holding ``model.pkl``) as one artifact.

        Stores ``model.pkl`` plus its ``model_meta.json`` (synthesized when
        absent) under a backend-specific location and returns the artifact URI
        to record in the registry: a filesystem path for the local backend,
        ``s3://<bucket>/<key>`` for S3. Idempotent: re-uploading overwrites.

        Raises:
            StorageError: when ``source`` has no ``model.pkl``, its metadata
                disagrees with ``model_id``/``version``, or the transfer fails.
        """
