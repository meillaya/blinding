"""Smoke test: every InferenceOps app package imports and exposes ``__version__``.

These imports ARE the assertion. A skeleton that fails to import (for example a
broken top-level ``import`` in any module) fails this test loudly, which is
what guards the Wave-2 seams built on top of these packages.
"""

from __future__ import annotations

import importlib

import inferenceops_gateway
import inferenceops_model_server
import inferenceops_worker

EXPECTED_VERSION = "0.1.0"

# Every todo-5 skeleton module that must import cleanly.
_SUBMODULES = (
    "inferenceops_gateway.auth",
    "inferenceops_gateway.config",
    "inferenceops_gateway.main",
    "inferenceops_gateway.rate_limit",
    "inferenceops_gateway.routes",
    "inferenceops_gateway.telemetry",
    "inferenceops_model_server.batching",
    "inferenceops_model_server.metrics",
    "inferenceops_model_server.model_loader",
    "inferenceops_model_server.predictor",
    "inferenceops_model_server.schemas",
    "inferenceops_model_server.server",
    "inferenceops_model_server.storage",
    "inferenceops_model_server.storage.base",
    "inferenceops_model_server.storage.local",
    "inferenceops_worker.batch_inference",
    "inferenceops_worker.drift_job",
    "inferenceops_worker.report_generator",
)


def test_packages_import_and_expose_version() -> None:
    """All three app packages import, pin ``__version__``, and load every skeleton."""
    assert inferenceops_gateway.__version__ == EXPECTED_VERSION
    assert inferenceops_model_server.__version__ == EXPECTED_VERSION
    assert inferenceops_worker.__version__ == EXPECTED_VERSION

    for name in _SUBMODULES:
        module = importlib.import_module(name)
        assert module.__name__ == name, f"imported {module.__name__!r}, expected {name!r}"
