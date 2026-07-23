"""OpenTelemetry tracing: init paths + one span per request (todo 34).

Mirrors the gateway's todo-11 telemetry tests, traces-only (the model server's
metrics story is Prometheus, in ``metrics.py``). Span assertions use an
in-memory exporter injected through ``create_app``'s ``telemetry`` seam — no
collector, no network. The unreachable-collector tests prove the OTLP path
degrades to log-and-drop instead of crashing the server.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final

import httpx
import pytest
from fastapi.testclient import TestClient
from inferenceops_model_server import model_loader
from inferenceops_model_server.server import ServerConfig, create_app
from inferenceops_model_server.telemetry import Telemetry, init_telemetry
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

FEATURE_COUNT: Final = 20
PAYLOAD: Mapping[str, object] = {"features": [0.1] * FEATURE_COUNT}


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
def span_exporter() -> InMemorySpanExporter:
    """Captures finished spans in-process (no collector involved)."""
    return InMemorySpanExporter()


@pytest.fixture()
def telemetry(span_exporter: InMemorySpanExporter) -> Telemetry:
    """In-memory telemetry: spans captured, nothing touches the network."""
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    return Telemetry(tracer_provider=tracer_provider)


@pytest.fixture()
def client(telemetry: Telemetry) -> Iterator[TestClient]:
    """A started default-config app with in-memory telemetry injected."""
    with TestClient(create_app(telemetry=telemetry)) as started:
        yield started


def test_span_created_per_predict_request(
    client: TestClient, span_exporter: InMemorySpanExporter
) -> None:
    response = _post_json(client, "/v1/predict", PAYLOAD)
    assert response.status_code == 200
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "POST /v1/predict"
    assert span.kind == SpanKind.SERVER
    attributes = span.attributes
    assert attributes is not None
    assert attributes["http.request.method"] == "POST"
    assert attributes["url.path"] == "/v1/predict"
    assert attributes["http.response.status_code"] == 200


def test_span_records_error_status_code(
    client: TestClient, span_exporter: InMemorySpanExporter
) -> None:
    """A handled 422 (feature-count mismatch) is still spanned, with its status."""
    response = _post_json(client, "/v1/predict", {"features": [0.1, 0.2, 0.3]})
    assert response.status_code == 422
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = spans[0].attributes
    assert attributes is not None
    assert attributes["http.response.status_code"] == 422


def test_init_without_endpoint_creates_working_span_only_provider() -> None:
    """No OTEL_ENDPOINT: the provider runs exporter-free; spans are recordable."""
    telemetry = init_telemetry("model-server-test")
    exporter = InMemorySpanExporter()
    telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = telemetry.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("manual-span"):
        pass
    finished = exporter.get_finished_spans()
    assert [span.name for span in finished] == ["manual-span"]
    telemetry.tracer_provider.shutdown()


def test_init_with_unreachable_collector_does_not_crash() -> None:
    """OTLP export against a dead collector is log-and-drop, never an exception."""
    # Port 9 (discard): nothing listens, so the connect fails fast and loudly.
    telemetry = init_telemetry("model-server-test", otel_endpoint="http://127.0.0.1:9")
    tracer = telemetry.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("would-be-exported"):
        pass
    # Force the batched export against the dead collector: must not raise.
    telemetry.tracer_provider.force_flush(timeout_millis=500)
    telemetry.tracer_provider.shutdown()


def test_create_app_reads_otel_endpoint_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """OTEL_ENDPOINT in the env wires the OTLP exporter; a dead one never crashes."""
    monkeypatch.setenv("OTEL_ENDPOINT", "http://127.0.0.1:9")
    with TestClient(create_app()) as started:
        response = _post_json(started, "/v1/predict", PAYLOAD)
    assert response.status_code == 200


def test_server_config_otel_endpoint_defaults_to_none() -> None:
    """Exporter-free by default: no OTEL_ENDPOINT means a span-only provider."""
    assert ServerConfig().otel_endpoint is None
