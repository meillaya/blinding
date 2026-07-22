"""DynamicBatcher tests: coalescing, size/window flush, timeout, cancel-safety, row isolation.

Flake avoidance (documented per adversarial class): every timing setup uses
>=5x margins — e.g. a 50ms window against 300ms inference for the timeout path,
and 5s caller deadlines against ~50ms flushes for success paths. No test
asserts success at the exact window boundary (default deadline == window is
exercised only on the failure path, where slow inference makes the outcome
deterministic), and no test asserts a wall-clock upper bound tighter than
100x the expected elapsed time. Inference-call counts are asserted under real
``asyncio.gather`` concurrency, never hardcoded per a schedule.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from inferenceops_model_server import model_loader
from inferenceops_model_server.batching import (
    BatchConfig,
    BatchResultCountMismatch,
    BatchTimeout,
    DynamicBatcher,
)
from inferenceops_model_server.errors import FeatureCountMismatch
from inferenceops_model_server.model_loader import TrainedModel
from inferenceops_model_server.predictor import Prediction, predict
from inferenceops_model_server.storage.local import LocalStorageBackend

MODEL_ID: Final = "fraud-detector"
MODEL_VERSION: Final = "1.0.0"
FEATURE_COUNT: Final = 20
_ARTIFACTS_ROOT: Final = Path(__file__).resolve().parents[3] / "models" / "artifacts"


class CountingInfer:
    """Fake batch inference: counts calls and returns a deterministic per-row result."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.calls = 0
        self.sizes: list[int] = []
        self._delay_s = delay_s

    def __call__(self, rows: list[list[float]]) -> list[float]:
        self.calls += 1
        self.sizes.append(len(rows))
        if self._delay_s:
            time.sleep(self._delay_s)  # runs in the batcher's worker thread
        return [sum(row) for row in rows]


class ArityCheckingInfer:
    """Mimics ``predictor.predict``: raises FeatureCountMismatch on wrong row arity."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rows: list[list[float]]) -> list[float]:
        self.calls += 1
        return [self._score(row) for row in rows]

    def _score(self, row: list[float]) -> float:
        if len(row) != FEATURE_COUNT:
            raise FeatureCountMismatch(expected=FEATURE_COUNT, actual=len(row))
        return sum(row)


async def _submit_catching[ResultT](
    batcher: DynamicBatcher[ResultT], features: list[float], timeout_s: float
) -> ResultT | Exception:
    """Await one submission, converting failure to a value for typed gathering."""
    try:
        return await batcher.submit(features, timeout_s=timeout_s)
    except Exception as exc:
        return exc


@pytest.fixture()
def trained_model() -> Iterator[TrainedModel]:
    """The real todo-6 classifier with an isolated loader cache per test."""
    model_loader.clear_cache()
    yield model_loader.load_model(LocalStorageBackend(_ARTIFACTS_ROOT), MODEL_ID, MODEL_VERSION)
    model_loader.clear_cache()


def test_config_rejects_nonpositive_batch_size() -> None:
    with pytest.raises(ValueError, match="max_batch_size"):
        BatchConfig(max_batch_size=0)


def test_config_rejects_negative_wait() -> None:
    with pytest.raises(ValueError, match="max_wait_ms"):
        BatchConfig(max_wait_ms=-1)


async def test_submit_rejects_negative_timeout() -> None:
    batcher = DynamicBatcher(BatchConfig(), CountingInfer())
    with pytest.raises(ValueError, match="timeout_s"):
        await batcher.submit([1.0], timeout_s=-1.0)


async def test_single_request_flushes_when_window_expires() -> None:
    infer = CountingInfer()
    batcher = DynamicBatcher(BatchConfig(max_batch_size=8, max_wait_ms=30), infer)
    started = time.perf_counter()
    result = await batcher.submit([2.5], timeout_s=5.0)
    elapsed = time.perf_counter() - started
    assert result == 2.5
    assert elapsed >= 0.02  # the window (30ms) triggered the flush, not size
    assert infer.calls == 1
    assert infer.sizes == [1]
    assert batcher.inference_count == 1


async def test_batch_flushes_at_max_size_before_window() -> None:
    infer = CountingInfer()
    batcher = DynamicBatcher(BatchConfig(max_batch_size=4, max_wait_ms=5000), infer)
    started = time.perf_counter()
    outcomes = await asyncio.gather(
        *(_submit_catching(batcher, [float(i)], 10.0) for i in range(4))
    )
    elapsed = time.perf_counter() - started
    assert list(outcomes) == [0.0, 1.0, 2.0, 3.0]
    assert elapsed < 2.0  # flushed by size at ~0ms, not by the 5s window
    assert infer.calls == 1
    assert infer.sizes == [4]


async def test_ten_concurrent_requests_coalesce_into_at_most_three_calls() -> None:
    infer = CountingInfer()
    batcher = DynamicBatcher(BatchConfig(max_batch_size=4, max_wait_ms=50), infer)
    outcomes = await asyncio.gather(
        *(_submit_catching(batcher, [float(i)], 5.0) for i in range(10))
    )
    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert errors == []
    assert list(outcomes) == [float(i) for i in range(10)]  # per-row fan-out intact
    assert 1 <= infer.calls <= 3  # ceil(10 / 4) inference calls
    assert sum(infer.sizes) == 10
    assert batcher.inference_count == infer.calls


async def test_timeout_raises_batch_timeout_when_inference_overruns_window() -> None:
    infer = CountingInfer(delay_s=0.3)  # 6x the 50ms window -> deterministic timeout
    batcher = DynamicBatcher(BatchConfig(max_batch_size=4, max_wait_ms=50), infer)
    with pytest.raises(BatchTimeout) as excinfo:
        await batcher.submit([1.0])  # default deadline = window = 50ms
    assert excinfo.value.timeout_s == 0.05
    await batcher.drain()
    assert infer.calls == 1  # the batch still ran; its result was discarded
    # cancel_resume: the timed-out caller did not corrupt the batcher
    assert await batcher.submit([7.0], timeout_s=5.0) == 7.0
    await batcher.drain()


async def test_timed_out_caller_does_not_starve_batch_mates() -> None:
    infer = CountingInfer(delay_s=0.1)
    batcher = DynamicBatcher(BatchConfig(max_batch_size=4, max_wait_ms=2000), infer)
    outcomes = await asyncio.gather(
        _submit_catching(batcher, [0.0], 5.0),
        _submit_catching(batcher, [99.0], 0.02),  # 5x shorter than inference
        _submit_catching(batcher, [2.0], 5.0),
        _submit_catching(batcher, [3.0], 5.0),
    )
    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], BatchTimeout)
    values = sorted(outcome for outcome in outcomes if not isinstance(outcome, Exception))
    assert values == [0.0, 2.0, 3.0]  # the other three batch mates were served
    assert infer.calls == 1  # one batch served all four rows
    assert infer.sizes == [4]
    assert await batcher.submit([5.0], timeout_s=5.0) == 5.0  # still usable


async def test_malformed_row_is_isolated_without_killing_batch() -> None:
    infer = ArityCheckingInfer()
    batcher = DynamicBatcher(BatchConfig(max_batch_size=4, max_wait_ms=50), infer)
    outcomes = await asyncio.gather(
        _submit_catching(batcher, [1.0] * FEATURE_COUNT, 5.0),
        _submit_catching(batcher, [1.0, 2.0, 3.0], 5.0),  # wrong arity
        _submit_catching(batcher, [2.0] * FEATURE_COUNT, 5.0),
        _submit_catching(batcher, [3.0] * FEATURE_COUNT, 5.0),
    )
    errors = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], FeatureCountMismatch)
    assert errors[0].expected == FEATURE_COUNT
    assert errors[0].actual == 3
    values = sorted(outcome for outcome in outcomes if not isinstance(outcome, Exception))
    assert values == [20.0, 40.0, 60.0]  # well-formed rows still served
    assert infer.calls == 5  # 1 batched call + 4 per-row isolation retries


async def test_isolation_skips_rows_whose_caller_already_timed_out() -> None:
    class BatchFailSlowSingleInfer:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, rows: list[list[float]]) -> list[float]:
            self.calls += 1
            if len(rows) > 1:
                raise ValueError("batch failure")
            time.sleep(0.05)
            return [sum(row) for row in rows]

    infer = BatchFailSlowSingleInfer()
    batcher = DynamicBatcher(BatchConfig(max_batch_size=4, max_wait_ms=30), infer)
    outcomes = await asyncio.gather(
        _submit_catching(batcher, [1.0], 0.01),  # gone before the 30ms window flush
        _submit_catching(batcher, [2.0], 5.0),
    )
    abandoned, served = outcomes[0], outcomes[1]
    assert isinstance(abandoned, BatchTimeout)
    assert served == 2.0
    assert infer.calls == 2  # 1 batch + 1 isolation retry (abandoned row skipped)


async def test_isolation_skips_caller_who_times_out_during_retry() -> None:
    class BatchFailSlowSingleInfer2:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, rows: list[list[float]]) -> list[float]:
            self.calls += 1
            if len(rows) > 1:
                raise ValueError("batch failure")
            time.sleep(0.1)  # the abandoned caller's deadline lands inside this
            return [sum(row) for row in rows]

    infer = BatchFailSlowSingleInfer2()
    batcher = DynamicBatcher(BatchConfig(max_batch_size=4, max_wait_ms=10), infer)
    outcomes = await asyncio.gather(
        # alive at the retry start (~10ms), gone by its end (~110ms)
        _submit_catching(batcher, [1.0], 0.05),
        _submit_catching(batcher, [2.0], 5.0),
    )
    abandoned, served = outcomes[0], outcomes[1]
    assert isinstance(abandoned, BatchTimeout)
    assert served == 2.0
    assert infer.calls == 3  # 1 batch + 2 retries; the abandoned result is discarded


async def test_isolation_surfaces_wrong_single_row_result_count() -> None:
    class BatchFailShortSingleInfer:
        def __call__(self, rows: list[list[float]]) -> list[float]:
            if len(rows) > 1:
                raise ValueError("batch failure")
            return []  # contract violation on the single-row retry

    batcher = DynamicBatcher(
        BatchConfig(max_batch_size=2, max_wait_ms=50), BatchFailShortSingleInfer()
    )
    outcomes = await asyncio.gather(
        _submit_catching(batcher, [1.0], 5.0),
        _submit_catching(batcher, [2.0], 5.0),
    )
    first, second = outcomes[0], outcomes[1]
    assert isinstance(first, BatchResultCountMismatch)
    assert isinstance(second, BatchResultCountMismatch)
    assert first.expected == 1
    assert first.actual == 0


async def test_whole_batch_fails_when_infer_returns_wrong_result_count() -> None:
    class ShortInfer:
        def __call__(self, rows: list[list[float]]) -> list[float]:
            return [sum(row) for row in rows[:-1]]  # contract violation: one short

    batcher = DynamicBatcher(BatchConfig(max_batch_size=2, max_wait_ms=50), ShortInfer())
    outcomes = await asyncio.gather(
        _submit_catching(batcher, [1.0], 5.0),
        _submit_catching(batcher, [2.0], 5.0),
    )
    assert len(outcomes) == 2
    first, second = outcomes[0], outcomes[1]
    assert isinstance(first, BatchResultCountMismatch)
    assert isinstance(second, BatchResultCountMismatch)
    assert first.expected == 2
    assert first.actual == 1


async def test_drain_flushes_pending_requests() -> None:
    batcher = DynamicBatcher(BatchConfig(max_batch_size=8, max_wait_ms=100), CountingInfer())
    task = asyncio.create_task(batcher.submit([3.0], timeout_s=5.0))
    await asyncio.sleep(0.02)  # queued, but the 100ms window has not expired
    assert batcher.inference_count == 0
    await batcher.drain()  # waits out the window and serves the queued request
    assert await task == 3.0


class ModelInfer:
    """Batch inference over the real model: one predictor pass per batch."""

    def __init__(self, model: TrainedModel) -> None:
        self._model = model
        self.calls = 0

    def __call__(self, rows: list[list[float]]) -> list[Prediction]:
        self.calls += 1
        return [predict(self._model, row) for row in rows]


async def test_real_model_batch_inference_matches_per_row_predict(
    trained_model: TrainedModel,
) -> None:
    infer = ModelInfer(trained_model)
    batcher = DynamicBatcher(BatchConfig(max_batch_size=8, max_wait_ms=30), infer)
    rows = [[float(i + 1)] * FEATURE_COUNT for i in range(5)]
    outcomes = await asyncio.gather(*(_submit_catching(batcher, row, 5.0) for row in rows))
    expected = [predict(trained_model, row) for row in rows]
    assert list(outcomes) == expected  # fan-out matches direct per-row prediction
    assert infer.calls == 1


async def test_real_model_wrong_arity_row_is_isolated(trained_model: TrainedModel) -> None:
    infer = ModelInfer(trained_model)
    batcher = DynamicBatcher(BatchConfig(max_batch_size=4, max_wait_ms=30), infer)
    good = [0.1] * FEATURE_COUNT
    bad = [0.1] * (FEATURE_COUNT - 1)
    outcomes = await asyncio.gather(
        _submit_catching(batcher, good, 5.0),
        _submit_catching(batcher, bad, 5.0),
    )
    good_outcome, bad_outcome = outcomes[0], outcomes[1]
    assert isinstance(good_outcome, Prediction)
    assert good_outcome == predict(trained_model, good)
    assert isinstance(bad_outcome, FeatureCountMismatch)
    assert bad_outcome.expected == FEATURE_COUNT
    assert bad_outcome.actual == FEATURE_COUNT - 1
