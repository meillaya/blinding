"""S3 storage backend for model artifacts (staging/prod; Local is dev).

Same metadata-driven resolution as ``LocalStorageBackend``: each artifact lives
under ``s3://<bucket>/<prefix>/<dir>/`` as ``model.pkl`` + ``model_meta.json``
(carrying ``model_id`` and ``model_version``), and a directory matches by the
identity recorded in its metadata, NOT by its name.

``endpoint_url`` injection (explicit arg > ``S3_ENDPOINT_URL`` env > default
AWS endpoint) lets moto/localstack and real S3 share one code path.
Credentials resolve through the default boto3 chain, so EKS IRSA (web
identity) and static ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` env keys
both work with no explicit configuration.

boto3/botocore ship no ``py.typed``, so the client is built behind a narrow
typed seam (the ``_S3Client`` protocol plus two suppressed construction
lines); every value entering the backend logic below is fully known — the same
boundary pattern as ``_sklearn_io.py``.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final, Protocol, TypedDict

import boto3  # pyright: ignore[reportMissingTypeStubs]  # boto3 ships no py.typed
from boto3.exceptions import Boto3Error  # pyright: ignore[reportMissingTypeStubs]
from botocore.exceptions import (  # pyright: ignore[reportMissingTypeStubs]
    BotoCoreError,
    ClientError,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from inferenceops_model_server.errors import ModelNotFound
from inferenceops_model_server.schemas import ModelInfo
from inferenceops_model_server.storage.base import (
    ModelStorage,
    StorageError,
    artifact_dirname,
    upload_meta_bytes,
)
from inferenceops_model_server.storage.local import ARTIFACT_FILENAME, META_FILENAME

ENDPOINT_URL_ENV_VAR: Final = "S3_ENDPOINT_URL"
_DEFAULT_CACHE_DIR_NAME: Final = "inferenceops-model-cache"

# StorageError moved to storage.base in todo 28 (the put_artifact contract is
# shared by both backends); it stays importable from this module for callers.
__all__ = ["ENDPOINT_URL_ENV_VAR", "S3StorageBackend", "StorageError"]


class _ModelMeta(BaseModel):
    """The identity fields every artifact's ``model_meta.json`` must carry.

    Mirrors ``local._ModelMeta``: the metadata convention is shared across
    backends, and ``local.py`` (todo 7) is not modified to expose it.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_id: str
    model_version: str


# --- typed seam over untyped boto3/botocore ---------------------------------


class _ObjectSummary(TypedDict):
    Key: str


class _ListObjectsV2Response(TypedDict, total=False):
    """boto3 omits ``Contents`` when empty and the token on the last page."""

    Contents: list[_ObjectSummary]
    IsTruncated: bool
    NextContinuationToken: str


class _StreamingBody(Protocol):
    def read(self) -> bytes: ...


class _GetObjectResponse(TypedDict):
    Body: _StreamingBody


class _S3Client(Protocol):
    """The boto3 S3 client surface the backend consumes (structural)."""

    def list_objects_v2(
        self, *, Bucket: str, Prefix: str, ContinuationToken: str = ...
    ) -> _ListObjectsV2Response: ...
    def get_object(self, *, Bucket: str, Key: str) -> _GetObjectResponse: ...
    def download_file(self, *, Bucket: str, Key: str, Filename: str) -> None: ...
    def upload_file(self, *, Filename: str, Bucket: str, Key: str) -> None: ...
    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> object: ...


def _build_client(endpoint_url: str | None) -> _S3Client:
    """Build the boto3 S3 client; default credential chain (IRSA / env keys).

    ``endpoint_url=None`` selects the default AWS endpoint (real S3).
    """
    client: _S3Client = (  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]
        boto3.client("s3", endpoint_url=endpoint_url)  # pyright: ignore[reportUnknownMemberType]
    )
    return client


# -----------------------------------------------------------------------------


def _resolve_endpoint_url(explicit: str | None) -> str | None:
    """Precedence: explicit arg > ``S3_ENDPOINT_URL`` env > None (default AWS).

    Blank values count as unset, mirroring ``MODELS_DIR`` handling in local.py.
    """
    if explicit is not None and explicit.strip():
        return explicit
    configured = os.environ.get(ENDPOINT_URL_ENV_VAR, "").strip()
    return configured or None


def _parse_model_info(raw: bytes) -> ModelInfo | None:
    """Parse one ``model_meta.json`` payload; return None when malformed."""
    try:
        meta = _ModelMeta.model_validate_json(raw)
    except ValidationError:
        return None
    return ModelInfo(model_id=meta.model_id, version=meta.model_version)


class S3StorageBackend(ModelStorage):
    """Resolve model artifacts under ``s3://<bucket>/<prefix>/``.

    Convention mirrors ``LocalStorageBackend``: each artifact directory holds
    ``model.pkl`` + ``model_meta.json``, and matching uses the identity
    recorded in the metadata, NOT the directory name — so ``fraud-detector``
    resolves under ``legacy_export_2024/`` with no name sanitization. Only
    directories exactly one level below the prefix count (the local backend's
    ``*/model_meta.json`` glob). A directory whose metadata matches but whose
    ``model.pkl`` key is absent counts as not found, as does malformed
    metadata (skipped when listing).

    ``get_artifact`` downloads into ``<cache_dir>/<bucket>/<dir>/model.pkl``:
    versioned artifacts are immutable, so a cached copy is never re-downloaded
    and a version can never be served from another version's cache path.

    Endpoint precedence: explicit ``endpoint_url`` arg > ``S3_ENDPOINT_URL``
    env var > default AWS endpoint. Credentials: default boto3 chain (EKS IRSA
    web identity and static env keys both resolve with no configuration).

    The constructor takes four configuration axes — bucket (identity), prefix
    (scope within the bucket), cache_dir (local filesystem), endpoint_url
    (network) — each independent; all but bucket have defaults.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        cache_dir: Path | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._cache_dir = (
            cache_dir
            if cache_dir is not None
            else Path(tempfile.gettempdir()) / _DEFAULT_CACHE_DIR_NAME
        )
        self._endpoint_url = _resolve_endpoint_url(endpoint_url)
        self._client = _build_client(self._endpoint_url)

    @property
    def endpoint_url(self) -> str | None:
        """The resolved S3 endpoint passed to boto3 (None = default AWS endpoint)."""
        return self._endpoint_url

    def get_artifact(self, model_id: str, version: str) -> Path:
        """Return the cached ``model.pkl`` path for ``model_id``/``version``.

        Raises:
            ModelNotFound: when the identity is not registered under the
                prefix, or its metadata matches but ``model.pkl`` is absent.
            StorageError: when the bucket is missing or the download fails.
        """
        keys = self._list_keys()
        for info, dirname in self._iter_artifacts(keys):
            if info.model_id == model_id and info.version == version:
                artifact_key = self._key(f"{dirname}/{ARTIFACT_FILENAME}")
                if artifact_key in keys:
                    return self._cached_artifact(dirname, artifact_key)
                raise ModelNotFound(model_id, version)
        raise ModelNotFound(model_id, version)

    def list_models(self) -> list[ModelInfo]:
        """List every model under the prefix, sorted by ``(model_id, version)``.

        Raises:
            StorageError: when the bucket is missing.
        """
        return sorted(
            (info for info, _ in self._iter_artifacts(self._list_keys())),
            key=lambda info: (info.model_id, info.version),
        )

    def put_artifact(self, model_id: str, version: str, source: Path) -> str:
        """Upload ``source``'s artifact under the prefix; return the ``s3://`` URI.

        Stores ``<prefix>/<dirname>/model.pkl`` and then its ``model_meta.json``:
        the metadata publishes the artifact to the metadata-driven resolution,
        so it lands last (a partial upload leaves the bytes invisible, never a
        phantom model). Idempotent: S3 overwrites both objects.

        Raises:
            StorageError: when ``source`` has no ``model.pkl``, its metadata
                disagrees with ``model_id``/``version``, or the upload fails
                (missing bucket, absent credentials, unreachable endpoint).
        """
        artifact = source / ARTIFACT_FILENAME
        if not artifact.is_file():
            raise StorageError(
                self._bucket, f"source directory {source} has no {ARTIFACT_FILENAME}"
            )
        meta_bytes = upload_meta_bytes(source, model_id, version)
        dirname = artifact_dirname(model_id, version)
        artifact_key = self._key(f"{dirname}/{ARTIFACT_FILENAME}")
        try:
            self._client.upload_file(
                Filename=str(artifact), Bucket=self._bucket, Key=artifact_key
            )
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._key(f"{dirname}/{META_FILENAME}"),
                Body=meta_bytes,
            )
        except (Boto3Error, BotoCoreError, ClientError) as exc:
            # Boto3Error: upload_file's transfer manager re-raises a failed
            # PutObject as S3UploadFailedError, outside the botocore hierarchy.
            raise StorageError(
                self._bucket, f"upload of {model_id}@{version} failed: {exc}"
            ) from exc
        return f"s3://{self._bucket}/{artifact_key}"

    def _key(self, relative: str) -> str:
        """Full object key for a path relative to the prefix."""
        return f"{self._prefix}/{relative}" if self._prefix else relative

    def _list_keys(self) -> set[str]:
        """Every object key under the prefix (paginated).

        Raises:
            StorageError: when the bucket is missing or listing fails.
        """
        keys: set[str] = set()
        token: str | None = None
        while True:
            try:
                if token is None:
                    response = self._client.list_objects_v2(
                        Bucket=self._bucket, Prefix=self._key("")
                    )
                else:
                    response = self._client.list_objects_v2(
                        Bucket=self._bucket, Prefix=self._key(""), ContinuationToken=token
                    )
            except ClientError as exc:
                raise StorageError(self._bucket, "listing objects failed") from exc
            for entry in response.get("Contents", []):
                keys.add(entry["Key"])
            if not response.get("IsTruncated", False):
                return keys
            token = response.get("NextContinuationToken")
            if token is None:
                return keys

    def _iter_artifacts(self, keys: set[str]) -> Iterator[tuple[ModelInfo, str]]:
        """Yield ``(info, dirname)`` for each parseable one-level-deep meta key."""
        strip = len(self._key(""))
        for key in sorted(keys):
            parts = key[strip:].split("/")
            # One level below the prefix only (mirrors the local ``*/`` glob);
            # S3 keys can carry empty segments a filesystem glob never would.
            if len(parts) != 2 or parts[1] != META_FILENAME or not parts[0]:
                continue
            info = _parse_model_info(self._read_meta(key))
            if info is not None:
                yield info, parts[0]

    def _read_meta(self, key: str) -> bytes:
        """Read one metadata object; a key raced away reads as malformed."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError:
            return b""
        return response["Body"].read()

    def _cached_artifact(self, dirname: str, artifact_key: str) -> Path:
        """Download the artifact once into the cache; immutable versions stick."""
        cached = self._cache_dir / self._bucket / dirname / ARTIFACT_FILENAME
        if cached.is_file():
            return cached
        cached.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(
                Bucket=self._bucket, Key=artifact_key, Filename=str(cached)
            )
        except ClientError as exc:
            raise StorageError(
                self._bucket, f"download of {artifact_key!r} failed"
            ) from exc
        return cached
