"""OpenTelemetry tracing + metrics for the gateway (todo 11).

``init_telemetry`` builds the process's tracer + meter providers: when
``OTEL_ENDPOINT`` is set, OTLP/HTTP exporters are attached (batched, exported
on background threads — an unreachable collector logs and drops, it never
crashes the gateway); otherwise the providers run exporter-free, so spans and
metrics are still created (and capturable in-process) while nothing touches
the network. ``TelemetryMiddleware`` opens one SERVER span per HTTP request —
method, path, and response status as attributes — and counts requests on a
meter, so the gateway's own /metrics story (todo 33) has OTel parity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SERVICE_NAME_KEY: Final = "service.name"
INSTRUMENTATION_NAME: Final = "inferenceops.gateway"
REQUEST_COUNTER_NAME: Final = "gateway.requests.total"


@dataclass(frozen=True, slots=True)
class Telemetry:
    """The process's telemetry providers (tracer + meter)."""

    tracer_provider: TracerProvider
    meter_provider: MeterProvider


def _otlp_processors(otel_endpoint: str) -> tuple[SpanProcessor, MetricReader]:
    """OTLP/HTTP exporters for one endpoint (signal paths appended per spec).

    Imported lazily: the exporter-free path (no ``OTEL_ENDPOINT``) never loads
    the exporter machinery at all.
    """
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    base = otel_endpoint.rstrip("/")
    return (
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")),
        PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{base}/v1/metrics")),
    )


def init_telemetry(service_name: str, otel_endpoint: str | None = None) -> Telemetry:
    """Initialise tracing + metrics; export via OTLP iff ``otel_endpoint`` is set.

    Never raises for an unreachable collector: OTLP export is batched on
    background threads and failures are logged and dropped by the SDK.
    """
    resource = Resource.create({SERVICE_NAME_KEY: service_name})
    tracer_provider = TracerProvider(resource=resource)
    metric_readers: list[MetricReader] = []
    if otel_endpoint is not None:
        span_processor, metric_reader = _otlp_processors(otel_endpoint)
        tracer_provider.add_span_processor(span_processor)
        metric_readers.append(metric_reader)
    meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
    # Best effort: the OTel API sets each global provider only once per process
    # (later calls log a warning). Callers that need a specific provider — e.g.
    # tests with an in-memory exporter — use the returned instance directly.
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    return Telemetry(tracer_provider=tracer_provider, meter_provider=meter_provider)


class TelemetryMiddleware:
    """Pure-ASGI middleware: one SERVER span + one counter increment per request."""

    def __init__(self, app: ASGIApp, telemetry: Telemetry) -> None:
        self.app = app
        self.tracer = telemetry.tracer_provider.get_tracer(INSTRUMENTATION_NAME)
        meter = telemetry.meter_provider.get_meter(INSTRUMENTATION_NAME)
        self.request_counter = meter.create_counter(
            REQUEST_COUNTER_NAME, description="HTTP requests through the gateway"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pass lifespan/websocket through untouched
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        method = request.method
        path = request.url.path
        attributes = {
            "http.request.method": method,
            "url.path": path,
            "url.scheme": request.url.scheme,
        }
        with self.tracer.start_as_current_span(
            f"{method} {path}", kind=SpanKind.SERVER, attributes=attributes
        ) as span:

            async def send_with_status(message: Message) -> None:
                message_type: object = message.get("type")
                if message_type == "http.response.start":
                    status_code: object = message.get("status")
                    if isinstance(status_code, int):
                        span.set_attribute("http.response.status_code", status_code)
                        counter_attributes = {
                            "http.request.method": method,
                            "http.response.status_code": str(status_code),
                        }
                        self.request_counter.add(1, counter_attributes)
                await send(message)

            # Unhandled exceptions propagate through the span context manager,
            # which records the exception and sets ERROR status on its own.
            await self.app(scope, receive, send_with_status)
