"""Data-drift detection job (stub)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DriftResult:
    """Outcome of a drift-detection run."""

    drifted: bool
    score: float


def detect_drift(reference_uri: str, current_uri: str) -> DriftResult:
    """Compare ``current_uri`` against ``reference_uri`` and report drift."""
    raise NotImplementedError
