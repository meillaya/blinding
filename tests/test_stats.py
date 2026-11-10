"""Tests for percentiles and the Wilson interval."""

from __future__ import annotations

import pytest

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
