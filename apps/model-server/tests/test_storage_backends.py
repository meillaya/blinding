"""Storage backend contract tests: LocalStorageBackend (real tempdir) + S3 (moto).

Both backends satisfy the SAME ``ModelStorage`` contract: metadata-driven
resolution (the identity in ``model_meta.json`` wins over the directory name),
sorted listing, typed ``ModelNotFound`` for an unknown model/version, and a
put/get round-trip whose bytes survive intact. S3-specific behavior is tested
against ``moto``'s ``mock_aws``: typed ``StorageError`` for a missing bucket,
``endpoint_url`` injection (``S3_ENDPOINT_URL`` env + explicit arg precedence),
download caching, and prefix scoping. boto3 ships no ``py.typed``, so raw
client calls in test setup carry narrow ``reportUnknownMemberType`` suppressions
(same convention as the gateway tests).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypedDict

import boto3  # pyright: ignore[reportMissingTypeStubs]
import pytest
from inferenceops_model_server.errors import ModelNotFound
from inferenceops_model_server.schemas import ModelInfo
from inferenceops_model_server.storage import (
    LocalStorageBackend,
    ModelStorage,
    S3StorageBackend,
)
from inferenceops_model_server.storage.local import ARTIFACT_FILENAME, META_FILENAME
from inferenceops_model_server.storage.s3 import ENDPOINT_URL_ENV_VAR, StorageError
from moto import mock_aws
from moto.moto_server.werkzeug_app import (  # pyright: ignore[reportMissingTypeStubs]
    DomainDispatcherApplication,
    create_backend_app,
)
from werkzeug.serving import make_server

MODEL_V1: Final = ModelInfo(model_id="fraud-detector", version="1.0.0")
MODEL_V2: Final = ModelInfo(model_id="fraud-detector", version="2.0.0")
MODEL_V3: Final = ModelInfo(model_id="fraud-detector", version="3.0.0")
BYTES_V1: Final = b"trained-classifier-bytes-v1\x00\x01"
BYTES_V2: Final = b"trained-classifier-bytes-v2\x02\x03"
BYTES_V3: Final = b"trained-classifier-bytes-v3\x04\x05"
BUCKET: Final = "test-bucket"


@dataclass(frozen=True, slots=True)
class Seed:
    """One artifact to stage before a test: directory name + identity + bytes."""

    dirname: str
    info: ModelInfo
    payload: bytes


SEED_V1: Final = Seed("fraud_detector_v1", MODEL_V1, BYTES_V1)
SEED_V2: Final = Seed("fraud_detector_v2", MODEL_V2, BYTES_V2)
SEED_V3: Final = Seed("fraud_detector_v3", MODEL_V3, BYTES_V3)


def _set_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake static keys + region so the default credential chain resolves instantly."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv(ENDPOINT_URL_ENV_VAR, raising=False)


class _TestStreamingBody(Protocol):
    def read(self) -> bytes: ...


class _TestGetObjectResponse(TypedDict):
    Body: _TestStreamingBody


class _TestS3Client(Protocol):
    """The setup-only S3 surface tests use to stage buckets and objects."""

    def create_bucket(self, *, Bucket: str) -> object: ...
    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> object: ...
    def get_object(self, *, Bucket: str, Key: str) -> _TestGetObjectResponse: ...


def _raw_s3_client(endpoint_url: str | None = None) -> _TestS3Client:
    """A boto3 client for test setup; the untyped seam stays inside this helper."""
    client: _TestS3Client = (  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]
        boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3", region_name="us-east-1", endpoint_url=endpoint_url
        )
    )
    return client


def _seed_local(root: Path, seed: Seed) -> None:
    """Write ``<root>/<dirname>/{model_meta.json,model.pkl}``."""
    directory = root / seed.dirname
    directory.mkdir(parents=True)
    meta = {"model_id": seed.info.model_id, "model_version": seed.info.version}
    (directory / META_FILENAME).write_text(json.dumps(meta), encoding="utf-8")
    (directory / ARTIFACT_FILENAME).write_bytes(seed.payload)


def _seed_s3(bucket: str, seed: Seed, endpoint_url: str | None = None) -> None:
    """Upload ``<dirname>/{model_meta.json,model.pkl}`` (dirname may carry slashes)."""
    meta = json.dumps(
        {"model_id": seed.info.model_id, "model_version": seed.info.version}
    ).encode()
    client = _raw_s3_client(endpoint_url)
    client.put_object(Bucket=bucket, Key=f"{seed.dirname}/{META_FILENAME}", Body=meta)
    client.put_object(
        Bucket=bucket, Key=f"{seed.dirname}/{ARTIFACT_FILENAME}", Body=seed.payload
    )


def _write_source(base: Path, seed: Seed, *, with_meta: bool = True) -> Path:
    """A source artifact directory for ``put_artifact`` (apart from any backend root)."""
    source = base / "upload-source" / seed.dirname
    source.mkdir(parents=True)
    if with_meta:
        meta = {"model_id": seed.info.model_id, "model_version": seed.info.version}
        (source / META_FILENAME).write_text(json.dumps(meta), encoding="utf-8")
    (source / ARTIFACT_FILENAME).write_bytes(seed.payload)
    return source


@pytest.fixture(params=["local", "s3"])
def seeded_storage(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ModelStorage]:
    """Both backends seeded identically: fraud-detector v1 + v2, distinct bytes."""
    if request.param == "local":
        root = tmp_path / "models"
        _seed_local(root, SEED_V1)
        _seed_local(root, SEED_V2)
        yield LocalStorageBackend(root)
        return
    with mock_aws():
        _set_aws_env(monkeypatch)
        _raw_s3_client().create_bucket(Bucket=BUCKET)
        _seed_s3(BUCKET, SEED_V1)
        _seed_s3(BUCKET, SEED_V2)
        yield S3StorageBackend(bucket=BUCKET, cache_dir=tmp_path / "cache")


@pytest.fixture
def s3_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Mocked AWS with an empty bucket created; yields the cache dir for backends."""
    with mock_aws():
        _set_aws_env(monkeypatch)
        _raw_s3_client().create_bucket(Bucket=BUCKET)
        yield tmp_path / "cache"


@pytest.fixture
def moto_server_endpoint(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A real moto HTTP server on an ephemeral port: the injected-endpoint path.

    ``mock_aws`` intercepts only default AWS endpoint URLs, so a custom
    ``endpoint_url`` needs a genuine HTTP mock — exactly what localstack or a
    moto server provides in dev — to exercise the identical production path.
    """
    app = DomainDispatcherApplication(create_backend_app)
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _set_aws_env(monkeypatch)
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


# --- shared ModelStorage contract (Local real tempdir + S3 via moto) ---------


def test_get_artifact_round_trip_returns_seeded_bytes(seeded_storage: ModelStorage) -> None:
    artifact = seeded_storage.get_artifact(MODEL_V1.model_id, MODEL_V1.version)
    assert artifact.is_file()
    assert artifact.read_bytes() == BYTES_V1


def test_get_artifact_resolves_each_version_without_stale_cache(
    seeded_storage: ModelStorage,
) -> None:
    first = seeded_storage.get_artifact(MODEL_V1.model_id, MODEL_V1.version)
    second = seeded_storage.get_artifact(MODEL_V2.model_id, MODEL_V2.version)
    assert first != second
    assert first.read_bytes() == BYTES_V1
    assert second.read_bytes() == BYTES_V2


def test_list_models_returns_every_seeded_version_sorted(seeded_storage: ModelStorage) -> None:
    assert seeded_storage.list_models() == [MODEL_V1, MODEL_V2]


def test_get_artifact_unknown_model_raises_model_not_found(seeded_storage: ModelStorage) -> None:
    with pytest.raises(ModelNotFound):
        seeded_storage.get_artifact("no-such-model", MODEL_V1.version)


def test_get_artifact_wrong_version_raises_model_not_found(seeded_storage: ModelStorage) -> None:
    with pytest.raises(ModelNotFound):
        seeded_storage.get_artifact(MODEL_V1.model_id, "9.9.9")


# --- S3-specific: metadata resolution, typed errors, caching, prefix ---------


def test_s3_metadata_driven_resolution_ignores_directory_name(s3_env: Path) -> None:
    seed = Seed("legacy_export_2024", ModelInfo(model_id="fraud-detector", version="3.0.0"), b"v3")
    _seed_s3(BUCKET, seed)
    backend = S3StorageBackend(bucket=BUCKET, cache_dir=s3_env)
    assert backend.get_artifact("fraud-detector", "3.0.0").read_bytes() == b"v3"


def test_s3_missing_artifact_key_raises_model_not_found(s3_env: Path) -> None:
    orphan_meta = json.dumps({"model_id": "orphan-model", "model_version": "1.0.0"}).encode()
    _raw_s3_client().put_object(
        Bucket=BUCKET, Key=f"orphan/{META_FILENAME}", Body=orphan_meta
    )
    backend = S3StorageBackend(bucket=BUCKET, cache_dir=s3_env)
    with pytest.raises(ModelNotFound):
        backend.get_artifact("orphan-model", "1.0.0")


def test_s3_malformed_meta_is_skipped(s3_env: Path) -> None:
    _raw_s3_client().put_object(
        Bucket=BUCKET, Key=f"broken/{META_FILENAME}", Body=b"{not json"
    )
    _seed_s3(BUCKET, SEED_V1)
    backend = S3StorageBackend(bucket=BUCKET, cache_dir=s3_env)
    assert backend.list_models() == [MODEL_V1]
    assert backend.get_artifact(MODEL_V1.model_id, MODEL_V1.version).read_bytes() == BYTES_V1


def test_s3_missing_bucket_raises_storage_error_on_get(s3_env: Path) -> None:
    backend = S3StorageBackend(bucket="wrong-bucket", cache_dir=s3_env)
    with pytest.raises(StorageError, match="wrong-bucket") as excinfo:
        backend.get_artifact(MODEL_V1.model_id, MODEL_V1.version)
    assert excinfo.value.location == "wrong-bucket"


def test_s3_missing_bucket_raises_storage_error_on_list(s3_env: Path) -> None:
    backend = S3StorageBackend(bucket="wrong-bucket", cache_dir=s3_env)
    with pytest.raises(StorageError, match="wrong-bucket"):
        backend.list_models()


def test_s3_second_get_serves_from_cache(s3_env: Path) -> None:
    _seed_s3(BUCKET, SEED_V1)
    backend = S3StorageBackend(bucket=BUCKET, cache_dir=s3_env)
    first = backend.get_artifact(MODEL_V1.model_id, MODEL_V1.version)
    # Mutate the object in S3: the immutable-version contract says the cached
    # copy keeps the original bytes, proving the second get skips re-download.
    _raw_s3_client().put_object(
        Bucket=BUCKET, Key=f"{SEED_V1.dirname}/{ARTIFACT_FILENAME}", Body=b"reuploaded"
    )
    second = backend.get_artifact(MODEL_V1.model_id, MODEL_V1.version)
    assert second == first
    assert second.read_bytes() == BYTES_V1


def test_s3_prefix_scopes_listing(s3_env: Path) -> None:
    scoped = Seed("prod/svc_a", ModelInfo(model_id="scoped-model", version="1.0.0"), b"scoped")
    decoy = Seed("other/svc_b", ModelInfo(model_id="decoy-model", version="1.0.0"), b"decoy")
    _seed_s3(BUCKET, scoped)
    _seed_s3(BUCKET, decoy)
    backend = S3StorageBackend(bucket=BUCKET, prefix="prod", cache_dir=s3_env)
    assert backend.list_models() == [scoped.info]
    assert backend.get_artifact("scoped-model", "1.0.0").read_bytes() == b"scoped"
    with pytest.raises(ModelNotFound):
        backend.get_artifact("decoy-model", "1.0.0")


# --- S3-specific: endpoint_url injection --------------------------------------


def test_s3_endpoint_url_env_injection_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moto_server_endpoint: str
) -> None:
    # Seed through the same injected endpoint the backend will use: the whole
    # round trip travels over real HTTP to the endpoint S3_ENDPOINT_URL names.
    monkeypatch.setenv(ENDPOINT_URL_ENV_VAR, moto_server_endpoint)
    _raw_s3_client(moto_server_endpoint).create_bucket(Bucket=BUCKET)
    _seed_s3(BUCKET, SEED_V1, endpoint_url=moto_server_endpoint)
    backend = S3StorageBackend(bucket=BUCKET, cache_dir=tmp_path / "cache")
    assert backend.endpoint_url == moto_server_endpoint
    assert backend.get_artifact(MODEL_V1.model_id, MODEL_V1.version).read_bytes() == BYTES_V1


def test_s3_explicit_endpoint_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moto_server_endpoint: str
) -> None:
    # A dead port in the env: if the env wrongly won over the explicit arg,
    # every request would connection-refuse instead of reaching the moto server.
    monkeypatch.setenv(ENDPOINT_URL_ENV_VAR, "http://127.0.0.1:1")
    _raw_s3_client(moto_server_endpoint).create_bucket(Bucket=BUCKET)
    _seed_s3(BUCKET, SEED_V1, endpoint_url=moto_server_endpoint)
    backend = S3StorageBackend(
        bucket=BUCKET, cache_dir=tmp_path / "cache", endpoint_url=moto_server_endpoint
    )
    assert backend.endpoint_url == moto_server_endpoint
    assert backend.get_artifact(MODEL_V1.model_id, MODEL_V1.version).read_bytes() == BYTES_V1


def test_s3_endpoint_defaults_to_none_and_round_trips(s3_env: Path) -> None:
    # _set_aws_env removed S3_ENDPOINT_URL: the default AWS endpoint path, which
    # moto intercepts, must share the exact same code path as injected endpoints.
    backend = S3StorageBackend(bucket=BUCKET, cache_dir=s3_env)
    assert backend.endpoint_url is None
    _seed_s3(BUCKET, SEED_V1)
    assert backend.get_artifact(MODEL_V1.model_id, MODEL_V1.version).read_bytes() == BYTES_V1


# --- put_artifact contract (Local real tempdir + S3 via moto) ----------------


def test_put_artifact_round_trip_resolves_by_metadata(
    seeded_storage: ModelStorage, tmp_path: Path
) -> None:
    source = _write_source(tmp_path, SEED_V3)
    uri = seeded_storage.put_artifact(MODEL_V3.model_id, MODEL_V3.version, source)
    assert uri.endswith(ARTIFACT_FILENAME)

    artifact = seeded_storage.get_artifact(MODEL_V3.model_id, MODEL_V3.version)
    assert artifact.read_bytes() == BYTES_V3
    assert seeded_storage.list_models() == [MODEL_V1, MODEL_V2, MODEL_V3]


def test_put_artifact_is_idempotent(seeded_storage: ModelStorage, tmp_path: Path) -> None:
    source = _write_source(tmp_path, SEED_V3)
    first = seeded_storage.put_artifact(MODEL_V3.model_id, MODEL_V3.version, source)
    second = seeded_storage.put_artifact(MODEL_V3.model_id, MODEL_V3.version, source)
    assert first == second
    artifact = seeded_storage.get_artifact(MODEL_V3.model_id, MODEL_V3.version)
    assert artifact.read_bytes() == BYTES_V3


def test_put_artifact_source_without_meta_synthesizes_identity(
    seeded_storage: ModelStorage, tmp_path: Path
) -> None:
    source = _write_source(tmp_path, SEED_V3, with_meta=False)
    seeded_storage.put_artifact(MODEL_V3.model_id, MODEL_V3.version, source)
    artifact = seeded_storage.get_artifact(MODEL_V3.model_id, MODEL_V3.version)
    assert artifact.read_bytes() == BYTES_V3


def test_put_artifact_meta_mismatch_raises_storage_error(
    seeded_storage: ModelStorage, tmp_path: Path
) -> None:
    source = _write_source(tmp_path, SEED_V3)  # source meta records 3.0.0
    with pytest.raises(StorageError, match="refusing to upload"):
        seeded_storage.put_artifact(MODEL_V3.model_id, "9.9.9", source)


def test_put_artifact_missing_artifact_raises_storage_error(
    seeded_storage: ModelStorage, tmp_path: Path
) -> None:
    source = tmp_path / "empty-source"
    source.mkdir()
    with pytest.raises(StorageError, match="has no model.pkl"):
        seeded_storage.put_artifact(MODEL_V3.model_id, MODEL_V3.version, source)


# --- S3-specific: put_artifact URI shape, meta fidelity, prefix, errors -------


def test_s3_put_artifact_returns_s3_uri_and_preserves_meta(
    s3_env: Path, tmp_path: Path
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    meta = {"model_id": "fraud-detector", "model_version": "3.0.0", "algorithm": "GBC"}
    (source / META_FILENAME).write_text(json.dumps(meta), encoding="utf-8")
    (source / ARTIFACT_FILENAME).write_bytes(BYTES_V3)
    backend = S3StorageBackend(bucket=BUCKET, cache_dir=s3_env)

    uri = backend.put_artifact("fraud-detector", "3.0.0", source)

    assert uri == f"s3://{BUCKET}/fraud-detector-3.0.0/{ARTIFACT_FILENAME}"
    # The source meta lands verbatim: provenance fields survive the upload.
    response = _raw_s3_client().get_object(
        Bucket=BUCKET, Key=f"fraud-detector-3.0.0/{META_FILENAME}"
    )
    assert json.loads(response["Body"].read()) == meta
    assert backend.get_artifact("fraud-detector", "3.0.0").read_bytes() == BYTES_V3


def test_s3_put_artifact_scopes_under_prefix(s3_env: Path, tmp_path: Path) -> None:
    source = _write_source(tmp_path, SEED_V3)
    backend = S3StorageBackend(bucket=BUCKET, prefix="prod", cache_dir=s3_env)
    uri = backend.put_artifact(MODEL_V3.model_id, MODEL_V3.version, source)
    assert uri == f"s3://{BUCKET}/prod/fraud-detector-3.0.0/{ARTIFACT_FILENAME}"
    assert backend.get_artifact(MODEL_V3.model_id, MODEL_V3.version).read_bytes() == BYTES_V3


def test_s3_put_artifact_missing_bucket_raises_storage_error(
    s3_env: Path, tmp_path: Path
) -> None:
    source = _write_source(tmp_path, SEED_V3)
    backend = S3StorageBackend(bucket="wrong-bucket", cache_dir=s3_env)
    with pytest.raises(StorageError, match="wrong-bucket") as excinfo:
        backend.put_artifact(MODEL_V3.model_id, MODEL_V3.version, source)
    assert excinfo.value.location == "wrong-bucket"


def test_s3_put_artifact_round_trips_via_injected_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moto_server_endpoint: str
) -> None:
    monkeypatch.setenv(ENDPOINT_URL_ENV_VAR, moto_server_endpoint)
    _raw_s3_client(moto_server_endpoint).create_bucket(Bucket=BUCKET)
    source = _write_source(tmp_path, SEED_V1)
    backend = S3StorageBackend(bucket=BUCKET, cache_dir=tmp_path / "cache")

    uri = backend.put_artifact(MODEL_V1.model_id, MODEL_V1.version, source)

    assert uri == f"s3://{BUCKET}/fraud-detector-1.0.0/{ARTIFACT_FILENAME}"
    assert backend.get_artifact(MODEL_V1.model_id, MODEL_V1.version).read_bytes() == BYTES_V1
