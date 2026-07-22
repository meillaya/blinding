"""S3 registry store tests against moto (same convention as todo 26's storage tests).

The S3 backend satisfies the SAME ``RegistryStore`` contract as the local one:
an absent registry reads as empty, save/load round-trips every entry, a
missing BUCKET raises a typed ``RegistryError`` (never fails open into an
empty state), and ``S3_ENDPOINT_URL`` injection follows the todo 26
precedence (explicit arg > env > default AWS endpoint). boto3 ships no
``py.typed``, so raw client calls in test setup carry the same narrow
suppressions as ``test_storage_backends.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final, Protocol

import boto3  # pyright: ignore[reportMissingTypeStubs]
import pytest
from moto import mock_aws
from registry_core import EvalMetrics, RegistryError, RegistryRecord, entry_key
from registry_store import S3_ENDPOINT_URL_ENV_VAR, S3RegistryStore

BUCKET: Final = "registry-test-bucket"


def _record() -> RegistryRecord:
    return RegistryRecord(
        model_id="fraud-detector",
        version="1.0.0",
        artifact_uri="models/artifacts/fraud_detector/model.pkl",
        framework="sklearn",
        metrics=EvalMetrics(accuracy=0.9655, f1=0.6102, roc_auc=0.9184),
        latency_p95_ms=None,
        approved_for_prod=False,
    )


class _TestS3Client(Protocol):
    def create_bucket(self, *, Bucket: str) -> object: ...


def _raw_s3_client() -> _TestS3Client:
    client: _TestS3Client = (  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]
        boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3", region_name="us-east-1"
        )
    )
    return client


def _set_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv(S3_ENDPOINT_URL_ENV_VAR, raising=False)


@pytest.fixture
def s3_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with mock_aws():
        _set_aws_env(monkeypatch)
        _raw_s3_client().create_bucket(Bucket=BUCKET)
        yield


def test_s3_store_round_trip(s3_env: None) -> None:
    store = S3RegistryStore(BUCKET)
    assert store.load() == {}

    entries = {entry_key("fraud-detector", "1.0.0"): _record()}
    store.save(entries)
    assert store.load() == entries


def test_s3_store_missing_bucket_raises_typed_error(s3_env: None) -> None:
    store = S3RegistryStore("no-such-bucket")
    with pytest.raises(RegistryError, match="no-such-bucket"):
        store.load()


def test_s3_endpoint_url_env_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(S3_ENDPOINT_URL_ENV_VAR, "http://127.0.0.1:9000")
    assert S3RegistryStore(BUCKET).endpoint_url == "http://127.0.0.1:9000"
    # Explicit argument wins over the env var (same precedence as todo 26).
    assert (
        S3RegistryStore(BUCKET, endpoint_url="http://127.0.0.1:9001").endpoint_url
        == "http://127.0.0.1:9001"
    )
