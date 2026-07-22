"""FastAPI model-server integration tests (spec §4 surface, todo 9).

Drives the real app through ``fastapi.testclient.TestClient``: every endpoint,
the 503→200 readiness transition, the error mappings (422 schema / 422 arity /
404 missing model / 504 batch timeout), the Prometheus exposition, per-app
metric isolation, and a real SIGTERM → exit-0 graceful-shutdown proof.

The ``_get``/``_post_json`` helpers are a typed seam over ``TestClient``: the
starlette/httpx compatibility shim exposes partially-Unknown request methods
under basedpyright strict, so those two calls carry a scoped suppression and
hand back a fully-typed ``httpx.Response`` — mirroring ``_sklearn_io.py`` on the
runtime side. Every assertion then runs on known types with zero suppressions.

Flake avoidance (documented per adversarial class): readiness is polled with a
bounded deadline (never a fixed sleep); the 504 path is made deterministic by a
predict deadline (10ms) far inside the coalescing window (300ms) so the request
always times out before the window flushes; the shutdown test spawns a real
uvicorn, polls readiness, sends SIGTERM, and bounds every wait, killing the
process in a ``finally`` so no orphan survives a failure. Each test clears the
module-level model-loader cache and builds its own app (dedicated
``MetricsRegistry``), so neither cached models nor Prometheus state leak.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Final

import httpx
import pytest
from fastapi.testclient import TestClient
from inferenceops_model_server import model_loader
from inferenceops_model_server.batching import BatchConfig
from inferenceops_model_server.schemas import BatchResponse, ModelInfo, PredictResponse
from inferenceops_model_server.server import ServerConfig, create_app
from inferenceops_model_server.storage.local import LocalStorageBackend

MODEL_ID: Final = "fraud-detector"
MODEL_VERSION: Final = "1.0.0"
FEATURE_COUNT: Final = 20
_SRC_ROOT: Final = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_ARTIFACTS_ROOT: Final = _REPO_ROOT / "models" / "artifacts"
_STARTUP_TIMEOUT_S: Final = 30.0
_SHUTDOWN_TIMEOUT_S: Final = 15.0
_KILL_TIMEOUT_S: Final = 10.0


def _get(client: TestClient, url: str) -> httpx.Response:
    """Typed seam: GET via TestClient, returning a fully-known httpx.Response."""
    return client.get(url)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def _post_json(client: TestClient, url: str, payload: Mapping[str, object]) -> httpx.Response:
    """Typed seam: POST JSON via TestClient, returning a fully-known httpx.Response."""
    return client.post(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        url, json=payload
    )


@pytest.fixture(autouse=True)
def clean_loader_cache() -> Iterator[None]:
    """Isolate each test from the module-level model-loader cache."""
    model_loader.clear_cache()
    yield
    model_loader.clear_cache()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A started default-config app (fraud-detector loaded, ready)."""
    with TestClient(create_app()) as started:
        yield started


def _features(value: float = 0.1, count: int = FEATURE_COUNT) -> list[float]:
    """A valid feature row of ``count`` finite floats."""
    return [value] * count


def _free_port() -> int:
    """Bind an ephemeral port and return its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _subprocess_env() -> dict[str, str]:
    """Env for the uvicorn subprocess: importable src-layout package + real artifact."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_SRC_ROOT) + (os.pathsep + existing if existing else "")
    env["MODELS_DIR"] = str(_ARTIFACTS_ROOT)
    return env


def _wait_ready(port: int, timeout_s: float) -> None:
    """Poll /readyz until it returns 200 or the bounded deadline expires."""
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/readyz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except OSError:  # connection refused / HTTP 503 / timeout: keep polling
            pass
        time.sleep(0.1)
    raise AssertionError(f"server on port {port} not ready within {timeout_s}s")


def test_predict_returns_valid_response(client: TestClient) -> None:
    response = _post_json(client, "/v1/predict", {"features": _features()})
    assert response.status_code == 200
    body = PredictResponse.model_validate(response.json())
    assert body.model_id == MODEL_ID
    assert body.model_version == MODEL_VERSION
    assert body.prediction in (0, 1)
    assert 0.0 <= body.score <= 1.0


def test_batch_returns_one_response_per_request(client: TestClient) -> None:
    payload: Mapping[str, object] = {
        "requests": [{"features": _features(v)} for v in (0.1, 0.2, 0.3)]
    }
    response = _post_json(client, "/v1/batch", payload)
    assert response.status_code == 200
    body = BatchResponse.model_validate(response.json())
    assert len(body.responses) == 3
    for item in body.responses:
        assert item.model_id == MODEL_ID
        assert item.model_version == MODEL_VERSION
        assert item.prediction in (0, 1)
        assert 0.0 <= item.score <= 1.0


def test_healthz_returns_200_when_up(client: TestClient) -> None:
    response = _get(client, "/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_transitions_503_to_200_across_startup() -> None:
    client = TestClient(create_app())
    # Before the lifespan runs the model is not loaded -> not ready.
    assert _get(client, "/readyz").status_code == 503
    # Entering the context runs startup (model load) -> ready.
    with client:
        assert _get(client, "/readyz").status_code == 200


def test_readyz_503_and_healthz_200_when_model_missing(tmp_path: Path) -> None:
    config = ServerConfig(storage=LocalStorageBackend(tmp_path))
    with TestClient(create_app(config)) as client:
        assert _get(client, "/healthz").status_code == 200  # alive...
        assert _get(client, "/readyz").status_code == 503  # ...but not ready


def test_metrics_exposes_prometheus_text(client: TestClient) -> None:
    _post_json(client, "/v1/predict", {"features": _features()})
    response = _get(client, "/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE inferenceops_request_count_total counter" in response.text
    assert "inferenceops_request_count" in response.text


def test_predict_records_request_duration_load_and_batch_metrics(client: TestClient) -> None:
    _post_json(client, "/v1/predict", {"features": _features()})
    text = _get(client, "/metrics").text
    assert (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="200"} 1.0' in text
    )
    assert 'inferenceops_request_duration_seconds_count{model_version="1.0.0"} 1.0' in text
    assert 'inferenceops_model_load_seconds_count{model_version="1.0.0"} 1.0' in text
    assert 'inferenceops_batch_size_bucket{le="1.0",model_version="1.0.0"} 1.0' in text


def test_models_lists_fraud_detector(client: TestClient) -> None:
    response = _get(client, "/v1/models")
    assert response.status_code == 200
    items: list[object] = response.json()
    models = [ModelInfo.model_validate(item) for item in items]
    assert ModelInfo(model_id=MODEL_ID, version=MODEL_VERSION) in models


def test_predict_invalid_schema_returns_422(client: TestClient) -> None:
    response = _post_json(client, "/v1/predict", {"features": ["not_a_number"]})
    assert response.status_code == 422


def test_predict_wrong_feature_count_returns_422(client: TestClient) -> None:
    response = _post_json(client, "/v1/predict", {"features": _features(count=3)})
    assert response.status_code == 422
    detail: object = response.json()["detail"]
    assert "expects 20 features" in str(detail)
    text = _get(client, "/metrics").text
    assert (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="422"} 1.0' in text
    )
    assert 'inferenceops_error_count_total{model_version="1.0.0"} 1.0' in text


def test_batch_with_wrong_arity_row_returns_422(client: TestClient) -> None:
    payload: Mapping[str, object] = {
        "requests": [{"features": _features()}, {"features": _features(count=3)}]
    }
    response = _post_json(client, "/v1/batch", payload)
    assert response.status_code == 422


def test_predict_model_not_found_returns_404(tmp_path: Path) -> None:
    config = ServerConfig(storage=LocalStorageBackend(tmp_path))
    with TestClient(create_app(config)) as client:
        response = _post_json(client, "/v1/predict", {"features": _features()})
        assert response.status_code == 404
        detail: object = response.json()["detail"]
        assert "not found" in str(detail)


def test_batch_model_not_found_returns_404(tmp_path: Path) -> None:
    config = ServerConfig(storage=LocalStorageBackend(tmp_path))
    with TestClient(create_app(config)) as client:
        response = _post_json(client, "/v1/batch", {"requests": [{"features": _features()}]})
        assert response.status_code == 404


def test_predict_batch_timeout_returns_504_and_records_error() -> None:
    # 10ms deadline sits far inside the 300ms window -> deterministic timeout.
    config = ServerConfig(
        batch_config=BatchConfig(max_batch_size=8, max_wait_ms=300), predict_timeout_s=0.01
    )
    with TestClient(create_app(config)) as client:
        response = _post_json(client, "/v1/predict", {"features": _features()})
        assert response.status_code == 504
        text = _get(client, "/metrics").text
        assert (
            'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
            'status="504"} 1.0' in text
        )
        assert 'inferenceops_error_count_total{model_version="1.0.0"} 1.0' in text


def test_metrics_do_not_leak_between_apps() -> None:
    sample = (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="200"} 1.0'
    )
    with TestClient(create_app()) as first:
        _post_json(first, "/v1/predict", {"features": _features()})
        assert sample in _get(first, "/metrics").text
    with TestClient(create_app()) as second:
        assert sample not in _get(second, "/metrics").text


def test_sigterm_graceful_shutdown_exits_zero() -> None:
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "inferenceops_model_server.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_ready(port, _STARTUP_TIMEOUT_S)
        proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=_KILL_TIMEOUT_S)
    output = proc.stdout.read() if proc.stdout is not None else ""
    assert returncode == 0, f"uvicorn exited {returncode} on SIGTERM; output:\n{output}"
