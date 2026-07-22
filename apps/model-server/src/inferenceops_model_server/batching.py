"""Dynamic request batching for inference.

``DynamicBatcher`` coalesces concurrently arriving requests so the model runs
once per batch instead of once per request:

- a batch flushes as soon as ``max_batch_size`` requests have accumulated, or
  when the oldest pending request has waited ``max_wait_ms`` (the coalescing
  window), whichever comes first;
- the whole batch is served by ONE call to the injected ``infer`` function
  (run off the event loop via ``asyncio.to_thread`` — sklearn inference is
  CPU-bound and must not block other callers), and per-row results are fanned
  back to each caller's future;
- ``submit`` enforces a per-request deadline (default: the coalescing window).
  A request not served in time fails with ``BatchTimeout``, which the server
  layer (todo 9) maps to HTTP 504. Callers that need headroom for the window
  PLUS inference time pass an explicit ``timeout_s``;
- a timed-out or cancelled caller never corrupts the batcher: its future is
  simply skipped when results fan out, and the batcher keeps serving;
- if the batched ``infer`` call raises (e.g. one row has the wrong feature
  count), the batcher retries each row of that batch alone, so a malformed
  row fails only its own caller with the original error while well-formed
  rows still get results. That fallback costs ``len(batch)`` extra inference
  calls, counted in ``inference_count``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass


class BatchTimeout(Exception):
    """Raised when a request is not served within its deadline (server maps to 504)."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        super().__init__(f"batch request not served within {timeout_s:.3f}s")


class BatchResultCountMismatch(Exception):
    """Raised when ``infer`` returns a result count that does not match the batch."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"infer returned {actual} results for {expected} rows")


@dataclass(frozen=True, slots=True)
class BatchConfig:
    """Tunables for the dynamic batcher."""

    max_batch_size: int = 8
    max_wait_ms: int = 50

    def __post_init__(self) -> None:
        if self.max_batch_size < 1:
            raise ValueError(f"max_batch_size must be >= 1, got {self.max_batch_size}")
        if self.max_wait_ms < 0:
            raise ValueError(f"max_wait_ms must be >= 0, got {self.max_wait_ms}")


@dataclass(frozen=True, slots=True)
class _PendingItem[ResultT]:
    """One queued request: its feature row and the future its caller awaits."""

    features: list[float]
    future: asyncio.Future[ResultT]


class DynamicBatcher[ResultT]:
    """Group concurrent requests into batches before running inference.

    ``infer`` maps a batch of feature rows to one result per row in a single
    inference call; it runs in a worker thread, so it may be synchronous
    CPU-bound code (e.g. ``predictor.predict`` over every row).

    ``inference_count`` is the number of ``infer`` invocations initiated
    (batched calls plus per-row isolation fallbacks) — the measure of how many
    times the model actually ran.
    """

    def __init__(
        self,
        config: BatchConfig,
        infer: Callable[[list[list[float]]], list[ResultT]],
    ) -> None:
        self._config = config
        self._infer = infer
        self._pending: list[_PendingItem[ResultT]] = []
        self._lock = asyncio.Lock()
        self._window_task: asyncio.Task[None] | None = None
        self._flush_tasks: set[asyncio.Task[None]] = set()
        self._inference_count = 0

    @property
    def inference_count(self) -> int:
        """Number of inference (``infer``) calls initiated so far."""
        return self._inference_count

    async def submit(self, features: list[float], timeout_s: float | None = None) -> ResultT:
        """Submit one feature row and await its result.

        The deadline defaults to the coalescing window (``max_wait_ms``); pass
        ``timeout_s`` to cover window + inference time. Raises ``BatchTimeout``
        when the deadline expires before the result arrives.
        """
        timeout = self._config.max_wait_ms / 1000.0 if timeout_s is None else timeout_s
        if timeout < 0:
            raise ValueError(f"timeout_s must be >= 0, got {timeout}")
        future: asyncio.Future[ResultT] = asyncio.get_running_loop().create_future()
        batch: list[_PendingItem[ResultT]] = []
        async with self._lock:
            self._pending.append(_PendingItem(features=list(features), future=future))
            if len(self._pending) >= self._config.max_batch_size:
                batch = self._pending
                self._pending = []
                self._cancel_window_locked()
            elif self._window_task is None:
                self._window_task = asyncio.create_task(self._window_flush())
        if batch:
            self._spawn_flush(batch)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise BatchTimeout(timeout) from None

    async def drain(self) -> None:
        """Wait until every queued request has been served (call after submissions stop)."""
        pending = [task for task in self._flush_tasks if not task.done()]
        window = self._window_task
        if window is not None and not window.done():
            pending.append(window)
        await asyncio.gather(*pending, return_exceptions=True)

    async def _window_flush(self) -> None:
        """Flush whatever is pending once the coalescing window expires."""
        await asyncio.sleep(self._config.max_wait_ms / 1000.0)
        async with self._lock:
            batch = self._pending
            self._pending = []
            self._window_task = None
        if batch:
            await self._run_batch(batch)

    def _cancel_window_locked(self) -> None:
        """Cancel the coalescing-window timer (caller holds ``_lock``)."""
        if self._window_task is not None:
            self._window_task.cancel()
            self._window_task = None

    def _spawn_flush(self, batch: list[_PendingItem[ResultT]]) -> None:
        """Run a size-triggered batch concurrently so the submitter can await."""
        task = asyncio.create_task(self._run_batch(batch))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _run_batch(self, batch: list[_PendingItem[ResultT]]) -> None:
        """Run one inference over ``batch`` and fan per-row results back."""
        rows = [item.features for item in batch]
        self._inference_count += 1
        try:
            results = await asyncio.to_thread(self._infer, rows)
        except Exception:
            # A bad row (e.g. wrong feature count) aborts the batched call;
            # retry rows alone so only the bad row's caller sees the error.
            await self._isolate_rows(batch)
            return
        if len(results) != len(batch):
            mismatch = BatchResultCountMismatch(expected=len(batch), actual=len(results))
            for item in batch:
                if not item.future.done():
                    item.future.set_exception(mismatch)
            return
        for item, result in zip(batch, results, strict=True):
            if not item.future.done():  # caller timed out / cancelled: skip
                item.future.set_result(result)

    async def _isolate_rows(self, batch: list[_PendingItem[ResultT]]) -> None:
        """Serve each row of a failed batch alone, isolating per-row errors."""
        for item in batch:
            if item.future.done():  # caller already gave up; skip the work
                continue
            self._inference_count += 1
            try:
                results = await asyncio.to_thread(self._infer, [item.features])
            except Exception as exc:
                if not item.future.done():
                    item.future.set_exception(exc)
                continue
            if item.future.done():
                continue
            if len(results) != 1:
                mismatch = BatchResultCountMismatch(expected=1, actual=len(results))
                item.future.set_exception(mismatch)
            else:
                item.future.set_result(results[0])
