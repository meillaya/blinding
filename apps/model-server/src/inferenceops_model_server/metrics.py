"""Prometheus metrics registry for the model server.

Every request-path metric carries a ``model_version`` label set from the loaded
model at request time — the discriminator the canary rollback (todo 30) uses to
compare candidate vs stable. Each ``MetricsRegistry`` owns a DEDICATED
``CollectorRegistry`` (never the process-global default), so instances never
leak state into one another: tests construct one per test.

``track_request`` is the recording helper the server (todo 9) wraps around
each handler: it times the request, records the duration, counts it under its
final HTTP ``status`` (the handler sets ``state.status`` — e.g. ``"504`` on a
handled ``BatchTimeout`` — and calls ``observe_error`` for failures), and, if
an exception escapes the block, forces status ``"500"`` and counts the error.

ML metrics (spec §M7, todo 35): ``observe_prediction`` records each served
prediction's label (``inferenceops_prediction_distribution``) and positive-class
confidence (``inferenceops_confidence_distribution``); ``set_model_info`` marks
the loaded model identity (``inferenceops_model_info``, labels ``model_id`` /
``version``); ``set_drift_score`` publishes the latest drift score computed by
the worker's offline ``drift_job`` (``inferenceops_drift_score``, unlabeled —
the alert threshold is global).
"""

from __future__ import annotations

import time
from contextlib import ContextDecorator
from dataclasses import dataclass
from types import TracebackType
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from inferenceops_model_server import model_loader

REQUEST_COUNT: Final = "inferenceops_request_count"
REQUEST_DURATION_SECONDS: Final = "inferenceops_request_duration_seconds"
ERROR_COUNT: Final = "inferenceops_error_count"
MODEL_LOAD_SECONDS: Final = "inferenceops_model_load_seconds"
BATCH_SIZE: Final = "inferenceops_batch_size"
PREDICTION_DISTRIBUTION: Final = "inferenceops_prediction_distribution"
CONFIDENCE_DISTRIBUTION: Final = "inferenceops_confidence_distribution"
MODEL_INFO: Final = "inferenceops_model_info"
DRIFT_SCORE: Final = "inferenceops_drift_score"

METRIC_NAMES: Final[tuple[str, ...]] = (
    REQUEST_COUNT,
    REQUEST_DURATION_SECONDS,
    ERROR_COUNT,
    MODEL_LOAD_SECONDS,
    BATCH_SIZE,
    PREDICTION_DISTRIBUTION,
    CONFIDENCE_DISTRIBUTION,
    MODEL_INFO,
    DRIFT_SCORE,
)

_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0,
)
_LOAD_BUCKETS: Final[tuple[float, ...]] = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_BATCH_BUCKETS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0)
# Binary labels split at 0.5: le="0.5" counts label 0, le="1.0" counts both.
_PREDICTION_BUCKETS: Final[tuple[float, ...]] = (0.5, 1.0)
_CONFIDENCE_BUCKETS: Final[tuple[float, ...]] = (
    0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0,
)


@dataclass(slots=True)
class RequestState:
    """Mutable handle a tracked request uses to report its final HTTP status."""

    status: str = "200"


class RequestTracker(ContextDecorator):
    """Time one request and record count + duration under its final status.

    Use as a context manager around (async) handlers — ``with
    metrics.track_request(...) as state`` — or as a decorator on sync
    functions. Create via ``MetricsRegistry.track_request``.
    """

    def __init__(self, metrics: MetricsRegistry, method: str, model_version: str) -> None:
        self._metrics = metrics
        self._method = method
        self._model_version = model_version
        self._state = RequestState()
        self._started = 0.0

    def __enter__(self) -> RequestState:
        self._state = RequestState()
        self._started = time.perf_counter()
        return self._state

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        elapsed = time.perf_counter() - self._started
        if exc_type is not None:
            self._state.status = "500"
            self._metrics.observe_error(self._model_version)
        self._metrics.observe_request(self._method, self._state.status, self._model_version)
        self._metrics.observe_duration(self._model_version, elapsed)
        return False  # never swallow the caller's exception


class MetricsRegistry:
    """Owns the Prometheus metrics exposed by the model server."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry if registry is not None else CollectorRegistry()
        self._request_count = Counter(
            REQUEST_COUNT,
            "Inference requests processed, by method, HTTP status and model version.",
            ("method", "status", "model_version"),
            registry=self._registry,
        )
        self._request_duration = Histogram(
            REQUEST_DURATION_SECONDS,
            "End-to-end request latency in seconds, by model version.",
            ("model_version",),
            registry=self._registry,
            buckets=_DURATION_BUCKETS,
        )
        self._error_count = Counter(
            ERROR_COUNT,
            "Failed requests (server errors, timeouts, bad input), by model version.",
            ("model_version",),
            registry=self._registry,
        )
        self._model_load_seconds = Histogram(
            MODEL_LOAD_SECONDS,
            "Wall-clock seconds the model artifact took to load, by model version.",
            ("model_version",),
            registry=self._registry,
            buckets=_LOAD_BUCKETS,
        )
        self._batch_size = Histogram(
            BATCH_SIZE,
            "Observed dynamic-batch sizes (rows per inference call), by model version.",
            ("model_version",),
            registry=self._registry,
            buckets=_BATCH_BUCKETS,
        )
        self._prediction_distribution = Histogram(
            PREDICTION_DISTRIBUTION,
            "Predicted class labels, by model version.",
            ("model_version",),
            registry=self._registry,
            buckets=_PREDICTION_BUCKETS,
        )
        self._confidence_distribution = Histogram(
            CONFIDENCE_DISTRIBUTION,
            "Positive-class prediction confidence (probability), by model version.",
            ("model_version",),
            registry=self._registry,
            buckets=_CONFIDENCE_BUCKETS,
        )
        self._model_info = Gauge(
            MODEL_INFO,
            "Identity of the model currently loaded (always 1).",
            ("model_id", "version"),
            registry=self._registry,
        )
        self._drift_score = Gauge(
            DRIFT_SCORE,
            "Latest data-drift score (PSI) from the worker's offline drift job.",
            registry=self._registry,
        )

    @property
    def registry(self) -> CollectorRegistry:
        """The dedicated collector registry backing these metrics."""
        return self._registry

    @property
    def content_type(self) -> str:
        """HTTP content type for the rendered exposition format (todo 9 /metrics)."""
        return CONTENT_TYPE_LATEST

    def observe_request(self, method: str, status: str, model_version: str) -> None:
        """Record a single inference request."""
        self._request_count.labels(method, status, model_version).inc()

    def observe_duration(self, model_version: str, seconds: float) -> None:
        """Record one request's end-to-end latency."""
        self._request_duration.labels(model_version).observe(seconds)

    def observe_error(self, model_version: str) -> None:
        """Record one failed request."""
        self._error_count.labels(model_version).inc()

    def observe_batch_size(self, model_version: str, size: int) -> None:
        """Record the row count of one inference call."""
        self._batch_size.labels(model_version).observe(float(size))

    def observe_model_load(self, model_version: str, seconds: float) -> None:
        """Record how long the model artifact took to load."""
        self._model_load_seconds.labels(model_version).observe(seconds)

    def observe_prediction(self, model_version: str, label: int, score: float) -> None:
        """Record one served prediction: its class label and positive-class confidence."""
        self._prediction_distribution.labels(model_version).observe(float(label))
        self._confidence_distribution.labels(model_version).observe(score)

    def set_model_info(self, model_id: str, version: str) -> None:
        """Mark ``(model_id, version)`` as the currently loaded model (info gauge = 1)."""
        self._model_info.labels(model_id, version).set(1.0)

    def set_drift_score(self, score: float) -> None:
        """Publish the latest drift score computed by the worker's drift job."""
        self._drift_score.set(score)

    def record_model_load(self, model_id: str, model_version: str) -> bool:
        """Record the cached load time from ``model_loader``; False if never loaded."""
        record = model_loader.load_record(model_id, model_version)
        if record is None:
            return False
        self.observe_model_load(model_version, record.load_seconds)
        return True

    def track_request(self, method: str, model_version: str) -> RequestTracker:
        """Return a recorder for one request (context manager or sync decorator)."""
        return RequestTracker(self, method, model_version)

    def render(self) -> str:
        """Render all metrics in the Prometheus text exposition format."""
        return generate_latest(self._registry).decode("utf-8")
