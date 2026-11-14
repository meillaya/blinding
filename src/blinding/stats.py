"""Latency percentiles and the pass-rate interval.

The benchmark reports p50, p95, and p99 from the requests it actually completed.
The eval reports a Wilson interval because a pass rate over twenty prompts is a
small sample, and a bare percentage hides that.

The compare command adds percentile-method bootstrap intervals: the run records
hold per-request and per-iteration samples, so a reported difference can carry
the sampling uncertainty of the two runs next to it. Resampling is seeded so two
reads of the same records produce the same interval.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_RESAMPLES = 2000
DEFAULT_CONFIDENCE = 0.95
DEFAULT_SEED = 0


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


@dataclass(frozen=True, slots=True)
class DeltaInterval:
    """Relative change of one statistic, with a bootstrap interval in percent.

    A `None` bound means the interval could not be formed, either because the
    baseline statistic was zero or because too many resamples were unusable.
    """

    point_pct: float | None
    low_pct: float | None
    high_pct: float | None
    resamples: int
    confidence: float


def _check_bootstrap(resamples: int, confidence: float) -> None:
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")


def _percentile_band(values: Sequence[float], confidence: float) -> tuple[float, float]:
    alpha = (1.0 - confidence) / 2.0
    return (percentile(values, alpha * 100.0), percentile(values, (1.0 - alpha) * 100.0))


def _relative_change(baseline: float, candidate: float) -> float | None:
    if baseline == 0.0:
        return None
    return (candidate - baseline) / baseline * 100.0


def _resample(values: Sequence[float], rng: random.Random) -> list[float]:
    count = len(values)
    return [values[rng.randrange(count)] for _ in range(count)]


def _pooled(groups: Sequence[Sequence[float]]) -> list[float]:
    pooled: list[float] = []
    for group in groups:
        pooled.extend(group)
    return pooled


def _resample_groups(groups: Sequence[Sequence[float]], rng: random.Random) -> list[float]:
    count = len(groups)
    pooled: list[float] = []
    for _ in range(count):
        pooled.extend(groups[rng.randrange(count)])
    return pooled


def bootstrap_percentile_ci(
    values: Sequence[float],
    p: float,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> tuple[float, float]:
    """Percentile-method bootstrap interval for one percentile of `values`."""
    if not values:
        raise ValueError("bootstrap needs at least one value")
    _check_bootstrap(resamples, confidence)
    ordered = tuple(values)
    rng = random.Random(seed)
    statistics = [percentile(_resample(ordered, rng), p) for _ in range(resamples)]
    return _percentile_band(statistics, confidence)


def bootstrap_delta_ci(
    baseline: Sequence[float],
    candidate: Sequence[float],
    p: float,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> DeltaInterval:
    """Relative delta of one percentile, resampling both sides independently."""
    if not baseline or not candidate:
        raise ValueError("bootstrap needs at least one value on each side")
    _check_bootstrap(resamples, confidence)
    base = tuple(baseline)
    cand = tuple(candidate)
    point = _relative_change(percentile(base, p), percentile(cand, p))
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(resamples):
        change = _relative_change(
            percentile(_resample(base, rng), p),
            percentile(_resample(cand, rng), p),
        )
        if change is not None:
            deltas.append(change)
    if len(deltas) * 2 < resamples:
        return DeltaInterval(point, None, None, resamples, confidence)
    low, high = _percentile_band(deltas, confidence)
    return DeltaInterval(point, low, high, resamples, confidence)


def cluster_bootstrap_delta_ci(
    baseline_groups: Sequence[Sequence[float]],
    candidate_groups: Sequence[Sequence[float]],
    p: float,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_SEED,
) -> DeltaInterval:
    """Relative delta of a pooled percentile, resampling whole groups.

    Inter-token gaps from one request are not independent, so the request is the
    resampling unit and the percentile is taken over the pooled gaps of the
    sampled requests.
    """
    base_groups = tuple(group for group in baseline_groups if group)
    cand_groups = tuple(group for group in candidate_groups if group)
    if not base_groups or not cand_groups:
        raise ValueError("cluster bootstrap needs at least one non-empty group")
    _check_bootstrap(resamples, confidence)
    point = _relative_change(
        percentile(_pooled(base_groups), p),
        percentile(_pooled(cand_groups), p),
    )
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(resamples):
        change = _relative_change(
            percentile(_resample_groups(base_groups, rng), p),
            percentile(_resample_groups(cand_groups, rng), p),
        )
        if change is not None:
            deltas.append(change)
    if len(deltas) * 2 < resamples:
        return DeltaInterval(point, None, None, resamples, confidence)
    low, high = _percentile_band(deltas, confidence)
    return DeltaInterval(point, low, high, resamples, confidence)
