"""OpenTelemetry: init paths + one span per request (todo 11).

Span assertions use an in-memory exporter injected through ``create_app``'s
``telemetry`` seam — no collector, no network. The unreachable-collector test
proves the OTLP path degrades to log-and-drop instead of crashing the gateway.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from inferenceops_gateway.config import GatewaySettings
from inferenceops_gateway.main import create_app
from inferenceops_gateway.telemetry import Telemetry, init_telemetry
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

PAYLOAD: Mapping[str, object] = {"features": [0.1] * 20}


def _post_json(
    client: TestClient,
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Typed seam: POST JSON via TestClient, returning a fully-known httpx.Response."""
    return client.post(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        url, json=payload, headers=headers
    )


def test_span_created_per_proxied_request(
    client: TestClient, span_exporter: InMemorySpanExporter, valid_key: str
) -> None:
    response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
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


def test_span_created_for_rejected_request_too(
    client: TestClient, span_exporter: InMemorySpanExporter
) -> None:
    """Telemetry is the OUTERMOST middleware: even a 401 from auth is spanned."""
    response = _post_json(client, "/v1/predict", PAYLOAD)  # no API key
    assert response.status_code == 401
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = spans[0].attributes
    assert attributes is not None
    assert attributes["http.response.status_code"] == 401


def test_request_counter_records_one_per_request(
    valid_key: str, mock_transport: httpx.MockTransport, span_exporter: InMemorySpanExporter
) -> None:
    reader = InMemoryMetricReader()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    meter_provider = MeterProvider(metric_readers=[reader])
    telemetry = Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)
    settings = GatewaySettings(api_keys=frozenset({valid_key}), rate_limit_rps=100.0)
    with TestClient(create_app(settings, transport=mock_transport, telemetry=telemetry)) as client:
        response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
        assert response.status_code == 200
        data = reader.get_metrics_data()
    assert data is not None
    metrics = [
        metric
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    ]
    counters = [metric for metric in metrics if metric.name == "gateway.requests.total"]
    assert len(counters) == 1
    counter_data = counters[0].data
    assert isinstance(counter_data, Sum)
    points = counter_data.data_points
    assert len(points) == 1
    point = points[0]
    assert point.value == 1
    point_attributes = point.attributes
    assert point_attributes is not None
    assert point_attributes["http.request.method"] == "POST"
    assert point_attributes["http.response.status_code"] == "200"


def test_span_records_unhandled_errors_without_crashing(
    span_exporter: InMemorySpanExporter, telemetry: Telemetry, valid_key: str
) -> None:
    """A non-HTTP upstream failure is recorded on the span, then surfaces as 500."""

    def exploding(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("kaboom")

    settings = GatewaySettings(api_keys=frozenset({valid_key}), rate_limit_rps=100.0)
    app = create_app(settings, transport=httpx.MockTransport(exploding), telemetry=telemetry)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
    assert response.status_code == 500
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert [event.name for event in spans[0].events] == ["exception"]


def test_init_without_endpoint_creates_working_span_only_provider() -> None:
    """No OTEL_ENDPOINT: providers run exporter-free; spans are still recordable."""
    telemetry = init_telemetry("gateway-test")
    exporter = InMemorySpanExporter()
    telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = telemetry.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("manual-span"):
        pass
    finished = exporter.get_finished_spans()
    assert [span.name for span in finished] == ["manual-span"]
    telemetry.tracer_provider.shutdown()
    telemetry.meter_provider.shutdown()


def test_init_with_unreachable_collector_does_not_crash() -> None:
    """OTLP export against a dead collector is log-and-drop, never an exception."""
    # Port 9 (discard): nothing listens, so the connect fails fast and loudly.
    telemetry = init_telemetry("gateway-test", otel_endpoint="http://127.0.0.1:9")
    tracer = telemetry.tracer_provider.get_tracer("test")
    with tracer.start_as_current_span("would-be-exported"):
        pass
    # Force the batched export against the dead collector: must not raise.
    telemetry.tracer_provider.force_flush(timeout_millis=500)
    telemetry.meter_provider.force_flush(timeout_millis=500)
    telemetry.tracer_provider.shutdown()
    telemetry.meter_provider.shutdown()


def test_module_level_app_is_fastapi() -> None:
    """The import-time ``app`` (built from ambient env) is a usable FastAPI app."""
    from inferenceops_gateway.main import app as module_app

    assert isinstance(module_app, FastAPI)
