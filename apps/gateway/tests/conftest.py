"""Pytest bootstrap + shared fixtures for gateway tests (todo 11).

The gateway is a uv workspace member with ``package = false`` (run from source,
never installed), so its importable package lives under ``apps/gateway/src/``;
prepend that root to ``sys.path``.

Shared fixtures keep every test off the real network: the model-server upstream
is an ``httpx.MockTransport`` that RECORDS each request it receives (so tests
assert what was actually forwarded), and telemetry is an in-memory span
exporter (so span assertions need no collector).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# The gateway runs from source (package = false): make its src root importable
# BEFORE the first-party imports below (the reason this bootstrap exists).
_SOURCE_ROOT: Final = Path(__file__).resolve().parents[1] / "src"

_source_root_str = str(_SOURCE_ROOT)
if _source_root_str not in sys.path:
    sys.path.insert(0, _source_root_str)

from inferenceops_gateway.config import GatewaySettings  # noqa: E402
from inferenceops_gateway.main import create_app  # noqa: E402
from inferenceops_gateway.telemetry import Telemetry  # noqa: E402

VALID_KEY: Final = "test-key-alpha"
SECOND_KEY: Final = "test-key-bravo"

# Canned model-server response bodies (distinct, so a proxy test that asserts
# the body proves it came from the mocked upstream, not from the gateway).
PREDICT_BODY: Final = {
    "prediction": 1,
    "score": 0.87,
    "model_id": "fraud-detector",
    "model_version": "1.0.0",
}
MODELS_BODY: Final = [{"model_id": "fraud-detector", "version": "1.0.0"}]


@pytest.fixture()
def valid_key() -> str:
    """A configured-valid API key."""
    return VALID_KEY


@pytest.fixture()
def second_key() -> str:
    """A second configured-valid API key (per-key isolation tests)."""
    return SECOND_KEY


@pytest.fixture()
def predict_body() -> dict[str, object]:
    """The mocked upstream's canned /v1/predict + /v1/batch body."""
    return dict(PREDICT_BODY)


@pytest.fixture()
def upstream_requests() -> list[httpx.Request]:
    """Every request the mocked model server receives, in order."""
    return []


@pytest.fixture()
def mock_transport(upstream_requests: list[httpx.Request]) -> httpx.MockTransport:
    """The mocked model-server upstream: records requests, returns canned bodies."""

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json=PREDICT_BODY)

    return httpx.MockTransport(handler)


@pytest.fixture()
def settings(valid_key: str, second_key: str) -> GatewaySettings:
    """Gateway settings with two known API keys (init kwargs beat ambient env)."""
    return GatewaySettings(api_keys=frozenset({valid_key, second_key}), rate_limit_rps=100.0)


@pytest.fixture()
def span_exporter() -> InMemorySpanExporter:
    """Captures finished spans in-process (no collector involved)."""
    return InMemorySpanExporter()


@pytest.fixture()
def telemetry(span_exporter: InMemorySpanExporter) -> Telemetry:
    """In-memory telemetry: spans captured, nothing touches the network."""
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return Telemetry(tracer_provider=tracer_provider, meter_provider=MeterProvider())


@pytest.fixture()
def client(
    settings: GatewaySettings,
    mock_transport: httpx.MockTransport,
    telemetry: Telemetry,
) -> Iterator[TestClient]:
    """A started gateway app wired to the mocked upstream + in-memory telemetry."""
    app = create_app(settings, transport=mock_transport, telemetry=telemetry)
    with TestClient(app) as started:
        yield started
