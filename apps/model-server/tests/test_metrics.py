"""MetricsRegistry tests: all 9 metrics render with their label sets.

Asserts on the REAL Prometheus text exposition format (``generate_latest``
output), never on prose: exact sample lines with their label sets and values.
Every test builds its own ``MetricsRegistry`` (dedicated CollectorRegistry),
so no Prometheus state leaks between tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from inferenceops_model_server import model_loader
from inferenceops_model_server.metrics import (
    BATCH_SIZE,
    ERROR_COUNT,
    METRIC_NAMES,
    MODEL_LOAD_SECONDS,
    REQUEST_COUNT,
    REQUEST_DURATION_SECONDS,
    MetricsRegistry,
)
from inferenceops_model_server.storage.local import LocalStorageBackend
from prometheus_client import CollectorRegistry

MODEL_ID: Final = "fraud-detector"
MODEL_VERSION: Final = "1.0.0"
_ARTIFACTS_ROOT: Final = Path(__file__).resolve().parents[3] / "models" / "artifacts"


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    """Isolate record_model_load tests from the module-level loader cache."""
    model_loader.clear_cache()
    yield
    model_loader.clear_cache()


def _sample(text: str, prefix: str) -> float:
    """Value of the first sample line starting with ``prefix``."""
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", maxsplit=1)[1])
    raise AssertionError(f"no sample line starting with {prefix!r}")


def test_all_nine_metrics_render() -> None:
    registry = MetricsRegistry()
    registry.observe_request("predict", "200", MODEL_VERSION)
    registry.observe_duration(MODEL_VERSION, 0.012)
    registry.observe_error(MODEL_VERSION)
    registry.observe_batch_size(MODEL_VERSION, 4)
    registry.observe_model_load(MODEL_VERSION, 0.35)
    registry.observe_prediction(MODEL_VERSION, 1, 0.97)
    registry.set_model_info(MODEL_ID, MODEL_VERSION)
    registry.set_drift_score(0.42)

    text = registry.render()
    for name in METRIC_NAMES:
        assert f"# TYPE {name}" in text
    for name in (
        REQUEST_COUNT,
        REQUEST_DURATION_SECONDS,
        ERROR_COUNT,
        MODEL_LOAD_SECONDS,
        BATCH_SIZE,
    ):
        labeled = [
            line
            for line in text.splitlines()
            if line.startswith(name) and f'model_version="{MODEL_VERSION}"' in line
        ]
        assert labeled, f"{name} has no sample carrying the model_version label"

    # Exact sample lines (labels render alphabetically in the exposition format).
    assert (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="200"} 1.0' in text
    )
    assert 'inferenceops_request_duration_seconds_count{model_version="1.0.0"} 1.0' in text
    assert 'inferenceops_error_count_total{model_version="1.0.0"} 1.0' in text
    assert 'inferenceops_model_load_seconds_count{model_version="1.0.0"} 1.0' in text
    assert 'inferenceops_batch_size_bucket{le="4.0",model_version="1.0.0"} 1.0' in text
    assert (
        'inferenceops_prediction_distribution_bucket{le="1.0",model_version="1.0.0"} 1.0' in text
    )
    assert (
        'inferenceops_confidence_distribution_bucket{le="0.99",model_version="1.0.0"} 1.0' in text
    )
    assert 'inferenceops_model_info{model_id="fraud-detector",version="1.0.0"} 1.0' in text
    assert "inferenceops_drift_score 0.42" in text


def test_observe_prediction_populates_label_and_confidence_histograms() -> None:
    registry = MetricsRegistry()
    registry.observe_prediction(MODEL_VERSION, 0, 0.03)
    registry.observe_prediction(MODEL_VERSION, 1, 0.97)
    text = registry.render()
    assert 'inferenceops_prediction_distribution_bucket{le="0.5",model_version="1.0.0"} 1.0' in text
    assert 'inferenceops_prediction_distribution_bucket{le="1.0",model_version="1.0.0"} 2.0' in text
    assert 'inferenceops_prediction_distribution_count{model_version="1.0.0"} 2.0' in text
    assert (
        'inferenceops_confidence_distribution_bucket{le="0.05",model_version="1.0.0"} 1.0' in text
    )
    assert (
        'inferenceops_confidence_distribution_bucket{le="0.95",model_version="1.0.0"} 1.0' in text
    )
    assert (
        'inferenceops_confidence_distribution_bucket{le="0.99",model_version="1.0.0"} 2.0' in text
    )
    assert 'inferenceops_confidence_distribution_count{model_version="1.0.0"} 2.0' in text


def test_set_model_info_marks_loaded_model_identity() -> None:
    registry = MetricsRegistry()
    registry.set_model_info(MODEL_ID, MODEL_VERSION)
    text = registry.render()
    assert "# TYPE inferenceops_model_info gauge" in text
    assert 'inferenceops_model_info{model_id="fraud-detector",version="1.0.0"} 1.0' in text


def test_set_drift_score_publishes_unlabeled_gauge() -> None:
    registry = MetricsRegistry()
    registry.set_drift_score(0.42)
    text = registry.render()
    assert "# TYPE inferenceops_drift_score gauge" in text
    assert "inferenceops_drift_score 0.42" in text


def test_accepts_caller_supplied_registry() -> None:
    supplied = CollectorRegistry()
    registry = MetricsRegistry(supplied)
    assert registry.registry is supplied
    registry.observe_error(MODEL_VERSION)
    assert 'inferenceops_error_count_total{model_version="1.0.0"} 1.0' in registry.render()


def test_registries_do_not_leak_between_instances() -> None:
    first = MetricsRegistry()
    first.observe_error(MODEL_VERSION)
    second = MetricsRegistry()
    sample = f'inferenceops_error_count_total{{model_version="{MODEL_VERSION}"}} 1.0'
    assert sample in first.render()
    assert sample not in second.render()


def test_request_count_partitions_by_label_set() -> None:
    registry = MetricsRegistry()
    registry.observe_request("predict", "200", MODEL_VERSION)
    registry.observe_request("predict", "200", MODEL_VERSION)
    registry.observe_request("batch", "504", MODEL_VERSION)
    text = registry.render()
    assert (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="200"} 2.0' in text
    )
    assert (
        'inferenceops_request_count_total{method="batch",model_version="1.0.0",'
        'status="504"} 1.0' in text
    )


def test_track_request_records_ok_status_and_duration() -> None:
    registry = MetricsRegistry()
    with registry.track_request("predict", MODEL_VERSION) as state:
        assert state.status == "200"
    text = registry.render()
    assert (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="200"} 1.0' in text
    )
    assert 'inferenceops_request_duration_seconds_count{model_version="1.0.0"} 1.0' in text
    duration_sum = _sample(
        text, 'inferenceops_request_duration_seconds_sum{model_version="1.0.0"} '
    )
    assert duration_sum > 0.0
    assert "inferenceops_error_count_total{" not in text  # no error sample recorded


def test_track_request_status_override_for_handled_failure() -> None:
    registry = MetricsRegistry()
    with registry.track_request("predict", MODEL_VERSION) as state:
        state.status = "504"  # the server's handled-BatchTimeout pattern (todo 9)
        registry.observe_error(MODEL_VERSION)
    text = registry.render()
    assert (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="504"} 1.0' in text
    )
    assert 'inferenceops_error_count_total{model_version="1.0.0"} 1.0' in text


def test_track_request_records_500_and_error_on_exception() -> None:
    registry = MetricsRegistry()
    with pytest.raises(RuntimeError, match="boom"):
        with registry.track_request("predict", MODEL_VERSION):
            raise RuntimeError("boom")
    text = registry.render()
    assert (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="500"} 1.0' in text
    )
    assert 'inferenceops_error_count_total{model_version="1.0.0"} 1.0' in text
    assert 'inferenceops_request_duration_seconds_count{model_version="1.0.0"} 1.0' in text


def test_tracker_works_as_decorator_on_sync_functions() -> None:
    registry = MetricsRegistry()

    @registry.track_request("predict", MODEL_VERSION)
    def handler() -> str:
        return "ok"

    assert handler() == "ok"
    assert (
        'inferenceops_request_count_total{method="predict",model_version="1.0.0",'
        'status="200"} 1.0' in registry.render()
    )


def test_record_model_load_reads_cached_load_seconds() -> None:
    model_loader.load_model(LocalStorageBackend(_ARTIFACTS_ROOT), MODEL_ID, MODEL_VERSION)
    registry = MetricsRegistry()
    assert registry.record_model_load(MODEL_ID, MODEL_VERSION) is True
    text = registry.render()
    assert 'inferenceops_model_load_seconds_count{model_version="1.0.0"} 1.0' in text
    load_sum = _sample(text, 'inferenceops_model_load_seconds_sum{model_version="1.0.0"} ')
    assert load_sum > 0.0


def test_record_model_load_returns_false_when_never_loaded() -> None:
    registry = MetricsRegistry()
    assert registry.record_model_load("never-loaded", "0.0.0") is False
    assert "inferenceops_model_load_seconds_count{" not in registry.render()


def test_batch_size_histogram_uses_integer_buckets() -> None:
    registry = MetricsRegistry()
    registry.observe_batch_size(MODEL_VERSION, 4)
    text = registry.render()
    assert 'inferenceops_batch_size_bucket{le="4.0",model_version="1.0.0"} 1.0' in text
    assert 'inferenceops_batch_size_bucket{le="2.0",model_version="1.0.0"} 0.0' in text


def test_content_type_is_prometheus_text_format() -> None:
    assert MetricsRegistry().content_type.startswith("text/plain")
