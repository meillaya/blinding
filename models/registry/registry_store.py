"""Registry metadata storage: local ``registry.json`` (dev) / S3 (prod).

The registry envelope (one JSON object keyed by ``<model_id>@<version>``)
needs read/write access, which the artifact-focused ``ModelStorage`` interface
(todo 7/26: ``get_artifact`` / ``list_models``) does not provide — so the
registry programs against its own two-method ``RegistryStore`` abstraction
with the SAME backend shape: a local filesystem backend for dev and an S3
backend for prod with ``S3_ENDPOINT_URL`` injection (explicit arg > env >
default AWS endpoint), so moto/localstack and real S3 share one code path and
credentials resolve through the default boto3 chain (EKS IRSA or static keys).

boto3/botocore ship no ``py.typed``, so the client is built and inspected
behind the same narrow typed seam convention as ``storage/s3.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final, Protocol, TypedDict, cast

import boto3  # pyright: ignore[reportMissingTypeStubs]  # boto3 ships no py.typed
from botocore.exceptions import (  # pyright: ignore[reportMissingTypeStubs]
    ClientError,
)
from registry_core import RegistryError, RegistryRecord, parse_registry, serialize_registry

DEFAULT_REGISTRY_FILE: Final = Path(__file__).resolve().parent / "registry.json"
S3_ENDPOINT_URL_ENV_VAR: Final = "S3_ENDPOINT_URL"
S3_REGISTRY_KEY: Final = "registry.json"
_MISSING_OBJECT_CODES: Final = frozenset({"NoSuchKey", "NotFound", "404"})


class RegistryStore(Protocol):
    """Read and write the whole registry envelope (load/save are atomic)."""

    def load(self) -> dict[str, RegistryRecord]:
        """Every registered entry; an absent registry reads as empty."""
        ...

    def save(self, entries: dict[str, RegistryRecord]) -> None:
        """Replace the whole envelope with ``entries``."""
        ...


class LocalRegistryStore:
    """The registry as one JSON file; a missing file is an empty registry."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else DEFAULT_REGISTRY_FILE

    @property
    def path(self) -> Path:
        """The registry file this store reads and writes."""
        return self._path

    def load(self) -> dict[str, RegistryRecord]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RegistryError(f"cannot read registry file {self._path}: {exc}") from exc
        return parse_registry(raw)

    def save(self, entries: dict[str, RegistryRecord]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(serialize_registry(entries))
        except OSError as exc:
            raise RegistryError(f"cannot write registry file {self._path}: {exc}") from exc


# --- typed seam over untyped boto3/botocore ---------------------------------


class _StreamingBody(Protocol):
    def read(self) -> bytes: ...


class _GetObjectResponse(TypedDict):
    Body: _StreamingBody


class _S3RegistryClient(Protocol):
    """The boto3 S3 client surface the registry consumes (structural)."""

    def get_object(self, *, Bucket: str, Key: str) -> _GetObjectResponse: ...
    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> object: ...


def _build_client(endpoint_url: str | None) -> _S3RegistryClient:
    """Build the boto3 S3 client; ``None`` selects the default AWS endpoint."""
    client: _S3RegistryClient = (  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]
        boto3.client("s3", endpoint_url=endpoint_url)  # pyright: ignore[reportUnknownMemberType]
    )
    return client


def _resolve_endpoint_url(explicit: str | None) -> str | None:
    """Precedence: explicit arg > ``S3_ENDPOINT_URL`` env > None (default AWS)."""
    if explicit is not None and explicit.strip():
        return explicit
    configured = os.environ.get(S3_ENDPOINT_URL_ENV_VAR, "").strip()
    return configured or None


class _ErrorDetail(TypedDict, total=False):
    Code: str


class _ClientErrorResponse(TypedDict, total=False):
    Error: _ErrorDetail


def _error_code(exc: ClientError) -> str:
    """Extract the S3 error code from an untyped botocore ``ClientError``.

    botocore ships no ``py.typed``, so ``exc.response`` crosses a typed seam:
    one ``cast`` at the boundary, fully known access below it.
    """
    response = cast(
        "_ClientErrorResponse",
        exc.response,  # pyright: ignore[reportUnknownMemberType]  # untyped botocore
    )
    return response.get("Error", {}).get("Code", "")


# -----------------------------------------------------------------------------


class S3RegistryStore:
    """The registry envelope as one S3 object (default key ``registry.json``).

    A missing OBJECT reads as an empty registry (first registration); a missing
    BUCKET (or any other S3 failure) raises a typed ``RegistryError`` — the
    registry never fails open into an empty state on a real error.
    """

    def __init__(
        self, bucket: str, *, endpoint_url: str | None = None, key: str = S3_REGISTRY_KEY
    ) -> None:
        self._bucket = bucket
        self._key = key
        self._endpoint_url = _resolve_endpoint_url(endpoint_url)
        self._client = _build_client(self._endpoint_url)

    @property
    def endpoint_url(self) -> str | None:
        """The resolved S3 endpoint passed to boto3 (None = default AWS endpoint)."""
        return self._endpoint_url

    def load(self) -> dict[str, RegistryRecord]:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key)
        except ClientError as exc:
            if _error_code(exc) in _MISSING_OBJECT_CODES:
                return {}
            raise RegistryError(
                f"S3 registry read on bucket {self._bucket!r} failed: {exc}"
            ) from exc
        return parse_registry(response["Body"].read())

    def save(self, entries: dict[str, RegistryRecord]) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=self._key, Body=serialize_registry(entries).encode()
            )
        except ClientError as exc:
            raise RegistryError(
                f"S3 registry write on bucket {self._bucket!r} failed: {exc}"
            ) from exc


def build_store(*, s3_bucket: str | None, registry_file: Path | None) -> RegistryStore:
    """Select the backend: S3 when a bucket is given, else the local JSON file."""
    if s3_bucket is not None and s3_bucket.strip():
        return S3RegistryStore(s3_bucket)
    return LocalRegistryStore(registry_file)
