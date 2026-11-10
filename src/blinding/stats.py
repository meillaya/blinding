"""Latency percentiles and the pass-rate interval.

The benchmark reports p50, p95, and p99 from the requests it actually completed.
The eval reports a Wilson interval because a pass rate over twenty prompts is a
small sample, and a bare percentage hides that.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Summary:
    """Summary of one list of measurements."""

    count: int
    mean: float
    p50: float
    p95: float
    p99: float
    minimum: float
    maximum: float

    def as_dict(self) -> dict[str, float | int]:
        """Plain mapping for the run record."""
        return {
            "count": self.count,
            "mean": self.mean,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "min": self.minimum,
            "max": self.maximum,
        }


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile for `p` between 0 and 100."""
    if not values:
        raise ValueError("percentile needs at least one value")
    if not 0.0 <= p <= 100.0:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * (p / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize(values: Sequence[float]) -> Summary:
    """Summary for a non-empty list of measurements."""
    if not values:
        raise ValueError("summarize needs at least one value")
    return Summary(
        count=len(values),
        mean=sum(values) / len(values),
        p50=percentile(values, 50.0),
        p95=percentile(values, 95.0),
        p99=percentile(values, 99.0),
        minimum=min(values),
        maximum=max(values),
    )


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a pass rate; returns (low, high) in [0, 1]."""
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("successes must be between zero and trials")
    if trials == 0:
        return (0.0, 0.0)
    proportion = successes / trials
    denominator = 1.0 + (z * z) / trials
    centre = (proportion + (z * z) / (2.0 * trials)) / denominator
    margin = (
        z
        * math.sqrt(
            (proportion * (1.0 - proportion) / trials) + ((z * z) / (4.0 * trials * trials))
        )
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))
