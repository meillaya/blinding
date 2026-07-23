"""Model server entrypoint: builds the FastAPI application (spec §4 surface).

Wires the todo-7 storage/loader/predictor and todo-8 batching/metrics into the
HTTP API:

- ``POST /v1/predict`` / ``POST /v1/batch`` — submit feature rows through the
  ``DynamicBatcher``. Every request that reaches inference is timed and counted
  via ``MetricsRegistry.track_request``; handled failures set the final status
  and record an error (``BatchTimeout`` → 504, ``FeatureCountMismatch`` → 422);
- ``GET /healthz`` — liveness: 200 once the process is up;
- ``GET /readyz`` — readiness: 503 until the model is loaded, 200 after;
- ``GET /metrics`` — Prometheus text exposition (``MetricsRegistry.render``);
- ``GET /v1/models`` — the storage backend's registered models.

The model is loaded once in the FastAPI lifespan; the ``model_version`` label on
every metric is read from the configured model (the canary discriminator, todo
30). On shutdown the batcher is drained so in-flight batches finish (pairs with
the k8s ``preStop`` hook, todo 16). A missing model leaves the process alive but
NOT ready (``/readyz`` 503, predict/batch 404) instead of crashing.

Graceful shutdown on SIGTERM: the lifespan installs its own SIGTERM handler that
marks the app not-ready, drains the batcher, then exits 0. This owns the exit
code because uvicorn (>=0.30) re-raises the captured signal after ITS graceful
shutdown, which would surface as exit 143/-15 to the supervisor; draining first
guarantees in-flight inference completes before the process exits 0.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from inferenceops_model_server import model_loader, predictor
from inferenceops_model_server.batching import BatchConfig, BatchTimeout, DynamicBatcher
from inferenceops_model_server.errors import FeatureCountMismatch, ModelNotFound
from inferenceops_model_server.metrics import MetricsRegistry, RequestState
from inferenceops_model_server.predictor import Prediction
from inferenceops_model_server.schemas import (
    BatchRequest,
    BatchResponse,
    ModelInfo,
    PredictRequest,
    PredictResponse,
)
from inferenceops_model_server.storage.base import ModelStorage
from inferenceops_model_server.storage.local import LocalStorageBackend
from inferenceops_model_server.telemetry import Telemetry, TelemetryMiddleware, init_telemetry

DEFAULT_MODEL_ID: Final = "fraud-detector"
DEFAULT_MODEL_VERSION: Final = "1.0.0"
DEFAULT_PREDICT_TIMEOUT_S: Final = 5.0
MODEL_ID_ENV_VAR: Final = "MODEL_ID"
MODEL_VERSION_ENV_VAR: Final = "MODEL_VERSION"
OTEL_ENDPOINT_ENV_VAR: Final = "OTEL_ENDPOINT"
SERVICE_NAME: Final = "inferenceops-model-server"


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Identity + tuning for one server instance; model identity is env-overridable."""

    model_id: str = DEFAULT_MODEL_ID
    model_version: str = DEFAULT_MODEL_VERSION
    batch_config: BatchConfig = field(default_factory=BatchConfig)
    predict_timeout_s: float = DEFAULT_PREDICT_TIMEOUT_S
    storage: ModelStorage | None = None  # None -> LocalStorageBackend()
    otel_endpoint: str | None = None  # None -> exporter-free tracer provider


@dataclass(slots=True)
class ServerState:
    """Mutable runtime state, fully initialized by the lifespan at startup."""

    storage: ModelStorage
    metrics: MetricsRegistry
    model_id: str
    model_version: str
    batcher: DynamicBatcher[Prediction] | None = None
    ready: bool = False


def _config_from_env() -> ServerConfig:
    """Config from the environment (model identity); defaults for everything else."""
    return ServerConfig(
        model_id=os.environ.get(MODEL_ID_ENV_VAR, DEFAULT_MODEL_ID),
        model_version=os.environ.get(MODEL_VERSION_ENV_VAR, DEFAULT_MODEL_VERSION),
        otel_endpoint=os.environ.get(OTEL_ENDPOINT_ENV_VAR),
    )


async def _predict_one(
    batcher: DynamicBatcher[Prediction], features: list[float], timeout_s: float
) -> Prediction | BatchTimeout | FeatureCountMismatch:
    """Submit one row; return its prediction or the batcher error it failed with."""
    try:
        return await batcher.submit(features, timeout_s=timeout_s)
    except (BatchTimeout, FeatureCountMismatch) as exc:
        return exc


def _to_response(prediction: Prediction, state: ServerState) -> PredictResponse:
    """Build the API response for one prediction under the loaded model's identity."""
    return PredictResponse(
        prediction=prediction.label,
        score=prediction.score,
        model_id=state.model_id,
        model_version=state.model_version,
    )


def _error_response(
    error: BatchTimeout | FeatureCountMismatch, state: ServerState, request_state: RequestState
) -> JSONResponse:
    """Map a handled batcher error to its HTTP status, recording the failure."""
    if isinstance(error, BatchTimeout):
        request_state.status = "504"
        status_code = 504
    else:  # FeatureCountMismatch — the only other type in the union
        request_state.status = "422"
        status_code = 422
    state.metrics.observe_error(state.model_version)
    return JSONResponse(status_code=status_code, content={"detail": str(error)})


async def _drain_and_exit(state: ServerState) -> None:
    """Finish in-flight batches, then exit 0 (the SIGTERM shutdown path)."""
    batcher = state.batcher
    if batcher is not None:
        await batcher.drain()
    os._exit(0)  # own the exit code: uvicorn re-raises the captured signal → 143


def _request_shutdown(state: ServerState) -> None:
    """SIGTERM callback (runs in the event loop): mark not-ready, drain, exit 0."""
    state.ready = False
    asyncio.get_running_loop().create_task(_drain_and_exit(state))


def _install_shutdown_handler(state: ServerState) -> None:
    """Install the SIGTERM handler when running under a real server (main thread).

    Skipped off the main thread (e.g. ``TestClient`` runs the lifespan loop in a
    worker thread, where signals cannot be registered and no SIGTERM is sent).
    """
    if threading.current_thread() is not threading.main_thread():
        return
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _request_shutdown, state)


def create_app(
    config: ServerConfig | None = None,
    *,
    telemetry: Telemetry | None = None,
) -> FastAPI:
    """Construct the model-server FastAPI application.

    ``telemetry`` is an injection seam: tests pass an in-memory-span provider;
    production leaves it None (OTLP export iff ``OTEL_ENDPOINT`` is configured).
    """
    cfg = config if config is not None else _config_from_env()
    tel = telemetry if telemetry is not None else init_telemetry(SERVICE_NAME, cfg.otel_endpoint)
    state = ServerState(
        storage=cfg.storage if cfg.storage is not None else LocalStorageBackend(),
        metrics=MetricsRegistry(),
        model_id=cfg.model_id,
        model_version=cfg.model_version,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        _install_shutdown_handler(state)
        try:
            model = model_loader.load_model(state.storage, cfg.model_id, cfg.model_version)
        except ModelNotFound:
            model = None  # stay alive but NOT ready: /readyz 503, predict/batch 404
        if model is not None:
            state.metrics.record_model_load(cfg.model_id, cfg.model_version)
            state.metrics.set_model_info(cfg.model_id, cfg.model_version)

            def infer(rows: list[list[float]]) -> list[Prediction]:
                state.metrics.observe_batch_size(cfg.model_version, len(rows))
                predictions = [predictor.predict(model, row) for row in rows]
                for prediction in predictions:
                    state.metrics.observe_prediction(
                        cfg.model_version, prediction.label, prediction.score
                    )
                return predictions

            state.batcher = DynamicBatcher(cfg.batch_config, infer)
            state.ready = True
        yield
        batcher = state.batcher
        if batcher is not None:
            await batcher.drain()  # graceful shutdown: finish in-flight batches

    app = FastAPI(title="InferenceOps Model Server", lifespan=lifespan)

    async def model_not_found_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def readyz() -> JSONResponse:
        if state.ready:
            return JSONResponse(content={"status": "ready"}, status_code=200)
        return JSONResponse(content={"status": "not ready"}, status_code=503)

    async def metrics_endpoint() -> Response:
        return Response(content=state.metrics.render(), media_type=state.metrics.content_type)

    async def list_models() -> list[ModelInfo]:
        return state.storage.list_models()

    async def predict_endpoint(request: PredictRequest) -> PredictResponse | JSONResponse:
        batcher = state.batcher
        if batcher is None:
            raise ModelNotFound(state.model_id, state.model_version)
        with state.metrics.track_request("predict", state.model_version) as request_state:
            result = await _predict_one(batcher, request.features, cfg.predict_timeout_s)
            if isinstance(result, (BatchTimeout, FeatureCountMismatch)):
                return _error_response(result, state, request_state)
            return _to_response(result, state)
        # basedpyright models the context manager's ``__exit__ -> bool`` as possibly
        # suppressing; track_request never does, so this path is unreachable.
        raise AssertionError("unreachable: track_request re-raises, never suppresses")

    async def batch_endpoint(request: BatchRequest) -> BatchResponse | JSONResponse:
        batcher = state.batcher
        if batcher is None:
            raise ModelNotFound(state.model_id, state.model_version)
        with state.metrics.track_request("batch", state.model_version) as request_state:
            results: list[Prediction | BatchTimeout | FeatureCountMismatch] = (
                await asyncio.gather(
                    *[
                        _predict_one(batcher, sub.features, cfg.predict_timeout_s)
                        for sub in request.requests
                    ]
                )
            )
            built: list[PredictResponse] = []
            for result in results:
                if isinstance(result, (BatchTimeout, FeatureCountMismatch)):
                    return _error_response(result, state, request_state)
                built.append(_to_response(result, state))
            return BatchResponse(responses=built)
        raise AssertionError("unreachable: track_request re-raises, never suppresses")

    # Explicit registration (vs. decorators) so each handler is a referenced name.
    app.add_exception_handler(ModelNotFound, model_not_found_handler)
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route("/readyz", readyz, methods=["GET"])
    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"])
    app.add_api_route("/v1/models", list_models, methods=["GET"], response_model=list[ModelInfo])
    app.add_api_route(
        "/v1/predict", predict_endpoint, methods=["POST"], response_model=PredictResponse
    )
    app.add_api_route("/v1/batch", batch_endpoint, methods=["POST"], response_model=BatchResponse)

    app.add_middleware(TelemetryMiddleware, telemetry=tel)
    return app


app = create_app()
