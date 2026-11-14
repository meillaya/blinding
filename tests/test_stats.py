"""Tests for percentiles and the Wilson interval."""

from __future__ import annotations

import pytest

from blinding import stats
from blinding.stats import percentile, summarize, wilson_interval


def test_percentile_interpolates() -> None:
    """The median of an even list sits halfway between the two middle values."""
    assert percentile([10.0, 20.0, 30.0, 40.0], 50.0) == pytest.approx(25.0)
    assert percentile([10.0, 20.0], 100.0) == 20.0


def test_percentile_rejects_bad_input() -> None:
    """An empty sample or an out-of-range p is a programming error."""
    with pytest.raises(ValueError):
        percentile([], 50.0)
    with pytest.raises(ValueError):
        percentile([1.0], 101.0)


def test_summarize_reports_extremes() -> None:
    """Every summary field describes the same sample."""
    summary = summarize([5.0, 1.0, 3.0])
    assert summary.count == 3
    assert summary.minimum == 1.0
    assert summary.maximum == 5.0
    assert summary.p50 == 3.0


def test_wilson_interval_stays_inside_zero_and_one() -> None:
    """A pass rate of 0 or 100 still has a bounded interval."""
    low, high = wilson_interval(20, 20)
    assert low <= 1.0
    assert high == pytest.approx(1.0)
    low, high = wilson_interval(0, 20)
    assert low == 0.0
    assert high < 0.2


def test_wilson_empty_trial_is_zero() -> None:
    """No cases means no information, not a perfect score."""
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_bootstrap_interval_is_deterministic() -> None:
    """The same seed and sample produce the same interval."""
    values = [10.0, 11.0, 9.0, 12.0, 10.5, 9.5, 11.5, 10.2]
    first = stats.bootstrap_percentile_ci(values, 50.0, resamples=200, seed=7)
    second = stats.bootstrap_percentile_ci(values, 50.0, resamples=200, seed=7)
    assert first == second
    assert first[0] <= first[1]


def test_bootstrap_delta_reports_a_regression() -> None:
    """A doubled sample pushes the relative delta interval above zero."""
    base = [10.0] * 12
    candidate = [20.0] * 12
    delta = stats.bootstrap_delta_ci(base, candidate, 50.0, resamples=500, seed=1)
    assert delta.point_pct == pytest.approx(100.0)
    assert delta.low_pct is not None and delta.low_pct > 0.0
    assert delta.high_pct is not None and delta.high_pct >= delta.low_pct


def test_bootstrap_delta_with_zero_baseline_has_no_interval() -> None:
    """A zero baseline statistic cannot carry a relative interval."""
    delta = stats.bootstrap_delta_ci([0.0] * 12, [1.0] * 12, 50.0, resamples=100)
    assert delta.point_pct is None
    assert delta.low_pct is None


def test_cluster_bootstrap_uses_ragged_groups() -> None:
    """Request groups of different sizes pool into one interval."""
    base = [[10.0] * 8, [11.0] * 4, [9.0] * 12]
    candidate = [[12.0] * 8, [13.0] * 4, [11.0] * 12]
    delta = stats.cluster_bootstrap_delta_ci(base, candidate, 50.0, resamples=300, seed=2)
    assert delta.point_pct is not None and delta.point_pct > 0.0
    assert delta.low_pct is not None and delta.high_pct is not None


def test_bootstrap_rejects_empty_input() -> None:
    """An empty sample is a programming error, not a zero interval."""
    with pytest.raises(ValueError):
        stats.bootstrap_percentile_ci([], 50.0)
    with pytest.raises(ValueError):
        stats.bootstrap_delta_ci([], [1.0], 50.0)
