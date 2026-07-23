"""OpenTelemetry tracing for the model server (todo 34).

Mirrors the gateway's todo-11 telemetry, traces only (the model server's metrics
story is Prometheus text exposition in ``metrics.py``): ``init_telemetry`` builds
the process's tracer provider; when ``OTEL_ENDPOINT`` is set an OTLP/HTTP
exporter is attached (batched, exported on a background thread — an unreachable
collector logs and drops, it never crashes the server); otherwise the provider
runs exporter-free, so spans are still created (and capturable in-process) while
nothing touches the network. ``TelemetryMiddleware`` opens one SERVER span per
HTTP request — method, path, and response status as attributes — so a prediction
through the stack is traceable gateway -> model server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SERVICE_NAME_KEY: Final = "service.name"
INSTRUMENTATION_NAME: Final = "inferenceops.model_server"


@dataclass(frozen=True, slots=True)
class Telemetry:
    """The process's tracer provider."""

    tracer_provider: TracerProvider


def _otlp_processor(otel_endpoint: str) -> SpanProcessor:
    """An OTLP/HTTP span exporter for one endpoint (signal path appended per spec).

    Imported lazily: the exporter-free path (no ``OTEL_ENDPOINT``) never loads
    the exporter machinery at all.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    base = otel_endpoint.rstrip("/")
    return BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))


def init_telemetry(service_name: str, otel_endpoint: str | None = None) -> Telemetry:
    """Initialise tracing; export via OTLP iff ``otel_endpoint`` is set.

    Never raises for an unreachable collector: OTLP export is batched on a
    background thread and failures are logged and dropped by the SDK.
    """
    resource = Resource.create({SERVICE_NAME_KEY: service_name})
    tracer_provider = TracerProvider(resource=resource)
    if otel_endpoint is not None:
        tracer_provider.add_span_processor(_otlp_processor(otel_endpoint))
    # Best effort: the OTel API sets the global provider only once per process
    # (later calls log a warning). Callers that need a specific provider — e.g.
    # tests with an in-memory exporter — use the returned instance directly.
    trace.set_tracer_provider(tracer_provider)
    return Telemetry(tracer_provider=tracer_provider)


class TelemetryMiddleware:
    """Pure-ASGI middleware: one SERVER span per HTTP request."""

    def __init__(self, app: ASGIApp, telemetry: Telemetry) -> None:
        self.app = app
        self.tracer = telemetry.tracer_provider.get_tracer(INSTRUMENTATION_NAME)

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
                await send(message)

            # Unhandled exceptions propagate through the span context manager,
            # which records the exception and sets ERROR status on its own.
            await self.app(scope, receive, send_with_status)
