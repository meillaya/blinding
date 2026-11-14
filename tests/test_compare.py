"""Tests for the compare engine and its verdicts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from blinding import compare
from blinding.errors import InvalidMeasurement

RunFactory = Callable[..., Path]
KernelFactory = Callable[..., Path]


def _flat(value: float, count: int = 12) -> list[float]:
    return [value] * count


def _groups(value: float, count: int = 12) -> list[list[float]]:
    return [[value] * 8 for _ in range(count)]


def test_identical_runs_report_unchanged(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """Two runs over the same samples produce no regression."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0)})
    cand = bench_run_factory(tmp_path, "b", ttfts={1: _flat(10.0)})
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    assert comparison.overall_verdict() == "unchanged"
    assert comparison.counts().get("regressed", 0) == 0
    assert compare.gate_exit_code(comparison) == 0


def test_latency_regression_is_detected(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """Doubled TTFT with no overlap in the interval fails the gate."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0)})
    cand = bench_run_factory(tmp_path, "b", ttfts={1: _flat(20.0)})
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    metric = next(m for m in comparison.metrics() if m.name == "ttft_p50_ms")
    assert metric.verdict == "regressed"
    assert metric.low_pct is not None and metric.low_pct > 5.0
    assert compare.gate_exit_code(comparison) == 1


def test_small_delta_is_unchanged(tmp_path: Path, bench_run_factory: RunFactory) -> None:
    """A one percent change sits inside the default tolerance."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0)})
    cand = bench_run_factory(tmp_path, "b", ttfts={1: _flat(10.1)})
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    metric = next(m for m in comparison.metrics() if m.name == "ttft_p50_ms")
    assert metric.verdict == "unchanged"
    assert compare.gate_exit_code(comparison) == 0


def test_invalid_candidate_blocks_the_gate(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """An invalid candidate run is unusable evidence, not a pass."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0)})
    cand = bench_run_factory(tmp_path, "b", ttfts={1: _flat(10.0)}, valid=False)
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    assert not comparison.compatibility.ok
    assert compare.gate_exit_code(comparison) == 3


def test_host_change_fails_unless_allowed(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """Runs from different hosts are not comparable by default."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0)})
    cand = bench_run_factory(
        tmp_path,
        "b",
        ttfts={1: _flat(10.0)},
        host={"hostname": "other-host", "cpu_model": "test-cpu"},
    )
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    assert not comparison.compatibility.ok
    assert compare.gate_exit_code(comparison) == 3
    allowed = compare.compare_runs(
        compare.load_run_data(base),
        compare.load_run_data(cand),
        compare.CompareOptions(allow_host_change=True),
    )
    assert allowed.compatibility.ok


def test_small_sample_is_inconclusive(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """Fewer samples than the minimum produce an inconclusive verdict."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0, count=4)})
    cand = bench_run_factory(tmp_path, "b", ttfts={1: _flat(20.0, count=4)})
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    metric = next(m for m in comparison.metrics() if m.name == "ttft_p50_ms")
    assert metric.verdict == "inconclusive"
    assert compare.gate_exit_code(comparison) == 0
    strict = compare.compare_runs(
        compare.load_run_data(base),
        compare.load_run_data(cand),
        compare.CompareOptions(fail_on_inconclusive=True),
    )
    assert compare.gate_exit_code(strict) == 1


def test_missing_samples_fall_back_to_the_record(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """A run without requests.csv compares point estimates and marks the source."""
    base = bench_run_factory(
        tmp_path,
        "a",
        ttfts={1: _flat(10.0)},
        write_requests=False,
    )
    cand = bench_run_factory(
        tmp_path,
        "b",
        ttfts={1: _flat(10.0)},
        write_requests=False,
    )
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    metric = next(m for m in comparison.metrics() if m.name == "ttft_p50_ms")
    assert metric.source == "record"
    assert metric.low_pct is None


def test_no_shared_levels_is_an_error(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """Comparing runs with disjoint concurrency levels raises."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0)})
    cand = bench_run_factory(tmp_path, "b", ttfts={8: _flat(10.0)})
    with pytest.raises(InvalidMeasurement, match="share no concurrency level"):
        compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))


def test_kernel_runs_compare_from_samples(
    tmp_path: Path, kernel_run_factory: KernelFactory
) -> None:
    """Kernel rows use the per-iteration samples and fail a real regression."""
    base = kernel_run_factory(
        tmp_path,
        "k1",
        copy=[{"variant": "vec4-i1-w4", "block": 256, "gbps": 400.0, "samples": [400.0] * 8}],
    )
    cand = kernel_run_factory(
        tmp_path,
        "k2",
        copy=[{"variant": "vec4-i1-w4", "block": 256, "gbps": 300.0, "samples": [300.0] * 8}],
    )
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    assert comparison.command == "kernel"
    assert comparison.rows[0].verdict == "regressed"
    assert compare.gate_exit_code(comparison) == 1
    payload = comparison.as_dict()
    assert payload["rows"][0]["benchmark"] == "copy"


def test_kernel_legacy_rows_without_samples_are_advisory(
    tmp_path: Path, kernel_run_factory: KernelFactory
) -> None:
    """A schema-1 kernel record compares point values without an interval."""
    base = kernel_run_factory(tmp_path, "k1", copy=[{"block": 128, "gbps": 400.0}])
    cand = kernel_run_factory(tmp_path, "k2", copy=[{"block": 128, "gbps": 300.0}])
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    assert comparison.rows[0].source == "record"
    assert comparison.rows[0].verdict == "point_regressed"
    assert compare.gate_exit_code(comparison) == 0
    strict = compare.compare_runs(
        compare.load_run_data(base),
        compare.load_run_data(cand),
        compare.CompareOptions(fail_on_point=True),
    )
    assert compare.gate_exit_code(strict) == 1


def test_profile_runs_are_not_gated(
    tmp_path: Path, kernel_run_factory: KernelFactory
) -> None:
    """Profile evidence is inspection material and cannot be compared."""
    base = kernel_run_factory(tmp_path, "p1", copy=[], command="profile")
    cand = kernel_run_factory(tmp_path, "p2", copy=[], command="profile")
    with pytest.raises(InvalidMeasurement, match="inspection evidence"):
        compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))


def test_json_output_is_deterministic_and_well_formed(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """The seeded bootstrap gives the same object twice and carries its schema."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0), 2: _flat(11.0)})
    cand = bench_run_factory(tmp_path, "b", ttfts={1: _flat(13.0), 2: _flat(11.5)})
    left = compare.load_run_data(base)
    right = compare.load_run_data(cand)
    first = compare.compare_runs(left, right).as_dict()
    second = compare.compare_runs(left, right).as_dict()
    assert first == second
    assert first["schema"] == compare.SCHEMA
    assert first["baseline"]["run_id"] == "a"
    assert len(first["levels"]) == 2
    assert first["gate"]["exit_code"] == 1


def test_table_rendering_has_all_rows(
    tmp_path: Path, bench_run_factory: RunFactory
) -> None:
    """The terminal and markdown renderers share one row per metric."""
    base = bench_run_factory(tmp_path, "a", ttfts={1: _flat(10.0)})
    cand = bench_run_factory(tmp_path, "b", ttfts={1: _flat(10.5)})
    comparison = compare.compare_runs(compare.load_run_data(base), compare.load_run_data(cand))
    rows = compare.table_rows(comparison)
    assert len(rows) == 5
    assert len(compare.table_headers(comparison)) == len(rows[0])
