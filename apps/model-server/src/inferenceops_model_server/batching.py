"""Dynamic request batching for inference (stub)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BatchConfig:
    """Tunables for the dynamic batcher."""

    max_batch_size: int = 8
    max_wait_ms: int = 50


class DynamicBatcher:
    """Group concurrent requests into batches before running inference."""

    def __init__(self, config: BatchConfig) -> None:
        self._config = config

    async def submit(self, features: list[float]) -> float:
        """Submit a single request and await its score."""
        raise NotImplementedError
