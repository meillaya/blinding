"""Tests for the rocprofv3 trace parser and profile summary.

The suite does not need rocprofv3 installed. The parser reads two small trace
files that mirror the tool's CSV output, one carrying an explicit duration
column and one carrying start and end timestamps, and the runner cases replace
the rocprofv3 lookup and the subprocess call with canned behavior.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from blinding import errors, profiler

FAKES = Path(__file__).resolve().parent / "fakes"
DURATION_TRACE = FAKES / "trace_duration.csv"
TIMESTAMP_TRACE = FAKES / "trace_timestamps.csv"

DURATION_TOTAL_S = 7.9e-05
TIMESTAMP_TOTAL_S = 8.5e-05


def _by_name(stats: list[profiler.KernelStat]) -> dict[str, profiler.KernelStat]:
    return {stat.name: stat for stat in stats}


def _stat(name: str, total_s: float) -> profiler.KernelStat:
    return profiler.KernelStat(
        name=name,
        calls=1,
        total_s=total_s,
        mean_s=total_s,
        p50_s=total_s,
        share=1.0,
    )


def _write_trace(tmp_path: Path, text: str, name: str = "trace.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _fake_profiler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    writes: Callable[[Path], None] | None = None,
    calls: list[tuple[list[str], dict[str, Any]]] | None = None,
) -> None:
    """Replace the rocprofv3 lookup and the subprocess call with canned behavior."""

    monkeypatch.setattr(profiler.shutil, "which", lambda name: "/opt/rocm/bin/rocprofv3")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append((command, kwargs))
        if writes is not None:
            writes(Path(command[command.index("-o") + 1]))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(profiler.subprocess, "run", run)


def _write_duration_trace(output_prefix: Path) -> None:
    """Write the duration fixture where rocprofv3 would put its kernel trace."""
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    target = output_prefix.parent / f"{output_prefix.name}_kernel_trace.csv"
    target.write_text(DURATION_TRACE.read_text(encoding="utf-8"), encoding="utf-8")


def test_duration_fixture_reports_calls_and_totals() -> None:
    """The explicit microsecond column gives the totals, not the timestamps."""
    stats = profiler.parse_kernel_trace(DURATION_TRACE)
    assert [stat.name for stat in stats] == ["mul_kernel", "add_kernel", "relu_kernel"]
    by_name = _by_name(stats)
    assert by_name["mul_kernel"].calls == 2
    assert by_name["add_kernel"].calls == 3
    assert by_name["relu_kernel"].calls == 1
    assert by_name["mul_kernel"].total_s == pytest.approx(4.0e-05)
    assert by_name["add_kernel"].total_s == pytest.approx(3.4e-05)
    assert by_name["relu_kernel"].total_s == pytest.approx(5.0e-06)
    assert sum(stat.total_s for stat in stats) == pytest.approx(DURATION_TOTAL_S)


def test_duration_fixture_reports_mean_and_median() -> None:
    stats = _by_name(profiler.parse_kernel_trace(DURATION_TRACE))
    assert stats["add_kernel"].mean_s == pytest.approx(3.4e-05 / 3.0)
    assert stats["add_kernel"].p50_s == pytest.approx(1.15e-05)
    assert stats["mul_kernel"].mean_s == pytest.approx(2.0e-05)
    assert stats["mul_kernel"].p50_s == pytest.approx(2.0e-05)
    assert stats["relu_kernel"].p50_s == pytest.approx(5.0e-06)


def test_duration_fixture_shares_sum_to_one() -> None:
    stats = _by_name(profiler.parse_kernel_trace(DURATION_TRACE))
    assert stats["mul_kernel"].share == pytest.approx(40.0 / 79.0)
    assert stats["add_kernel"].share == pytest.approx(34.0 / 79.0)
    assert stats["relu_kernel"].share == pytest.approx(5.0 / 79.0)
    assert sum(stat.share for stat in stats.values()) == pytest.approx(1.0)


def test_timestamp_fixture_derives_durations() -> None:
    """Without a duration column, the gap between the timestamps is the duration."""
    stats = profiler.parse_kernel_trace(TIMESTAMP_TRACE)
    assert [stat.name for stat in stats] == ["add_kernel", "relu_kernel", "mul_kernel"]
    by_name = _by_name(stats)
    assert by_name["add_kernel"].calls == 2
    assert by_name["relu_kernel"].calls == 1
    assert by_name["mul_kernel"].calls == 2
    assert by_name["add_kernel"].total_s == pytest.approx(4.0e-05)
    assert by_name["relu_kernel"].total_s == pytest.approx(2.5e-05)
    assert by_name["mul_kernel"].total_s == pytest.approx(2.0e-05)
    assert sum(stat.total_s for stat in stats) == pytest.approx(TIMESTAMP_TOTAL_S)


def test_timestamp_fixture_reports_median() -> None:
    stats = _by_name(profiler.parse_kernel_trace(TIMESTAMP_TRACE))
    assert stats["add_kernel"].p50_s == pytest.approx(2.0e-05)
    assert stats["mul_kernel"].p50_s == pytest.approx(1.0e-05)
    assert stats["relu_kernel"].p50_s == pytest.approx(2.5e-05)


def test_timestamp_fixture_shares_sum_to_one() -> None:
    stats = _by_name(profiler.parse_kernel_trace(TIMESTAMP_TRACE))
    assert stats["add_kernel"].share == pytest.approx(40.0 / 85.0)
    assert stats["relu_kernel"].share == pytest.approx(25.0 / 85.0)
    assert stats["mul_kernel"].share == pytest.approx(20.0 / 85.0)
    assert sum(stat.share for stat in stats.values()) == pytest.approx(1.0)


ALIAS_TRACES: list[tuple[str, dict[str, tuple[int, float]]]] = [
    (
        "KernelName,Duration\nadd_kernel,1000\nadd_kernel,3000\nmul_kernel,2000\n",
        {"add_kernel": (2, 4.0e-06), "mul_kernel": (1, 2.0e-06)},
    ),
    (
        '"Name","Duration_Ns"\n"add_kernel",500\n"relu_kernel",1500\n',
        {"add_kernel": (1, 5.0e-07), "relu_kernel": (1, 1.5e-06)},
    ),
    (
        '"Kernel_Name","Duration_NS"\n"add_kernel",800\n',
        {"add_kernel": (1, 8.0e-07)},
    ),
    (
        "kernel_name,duration_us\nadd_kernel,10.5\nadd_kernel,4.5\n",
        {"add_kernel": (2, 1.5e-05)},
    ),
    (
        '"Kernel_Name","End_Timestamp","Start_Timestamp"\n"add_kernel",2500,500\n',
        {"add_kernel": (1, 2.0e-06)},
    ),
    (
        '"Name","Start_Timestamp_Us","End_Timestamp_Us"\n"add_kernel",100,130\n',
        {"add_kernel": (1, 3.0e-05)},
    ),
]


@pytest.mark.parametrize(("text", "expected"), ALIAS_TRACES)
def test_header_aliases_are_accepted(
    tmp_path: Path, text: str, expected: dict[str, tuple[int, float]]
) -> None:
    stats = _by_name(profiler.parse_kernel_trace(_write_trace(tmp_path, text)))
    assert set(stats) == set(expected)
    for name, (calls, total_s) in expected.items():
        assert stats[name].calls == calls
        assert stats[name].total_s == pytest.approx(total_s)


def test_explicit_duration_wins_over_timestamps(tmp_path: Path) -> None:
    text = 'Kernel_Name,Duration,Start_Timestamp,End_Timestamp\nadd_kernel,1000,0,999999\n'
    stats = profiler.parse_kernel_trace(_write_trace(tmp_path, text))
    assert stats[0].total_s == pytest.approx(1.0e-06)


def test_rows_without_a_name_are_skipped(tmp_path: Path) -> None:
    text = 'Kernel_Name,Duration\n,1000\nadd_kernel,2000\n,\n'
    stats = profiler.parse_kernel_trace(_write_trace(tmp_path, text))
    assert [stat.name for stat in stats] == ["add_kernel"]
    assert stats[0].calls == 1
    assert stats[0].total_s == pytest.approx(2.0e-06)


BAD_TRACES = [
    pytest.param("Start_Timestamp,End_Timestamp\n1,2\n", "name column", id="no-name-column"),
    pytest.param(
        "Kernel_Name,Dispatch_Id\nadd_kernel,1\n", "duration data", id="no-duration-data"
    ),
    pytest.param('"Kernel_Name","Duration"\n"add_kernel",\n', "no value", id="empty-duration"),
    pytest.param("Kernel_Name,Duration\nadd_kernel,fast\n", "non-numeric", id="bad-duration"),
    pytest.param(
        "Kernel_Name,Start_Timestamp,End_Timestamp\nadd_kernel,20,10\n",
        "before it starts",
        id="end-before-start",
    ),
    pytest.param("Kernel_Name,Duration\n", "duration data rows", id="header-only"),
    pytest.param("", "empty", id="empty-file"),
]


@pytest.mark.parametrize(("text", "match"), BAD_TRACES)
def test_bad_trace_raises_upstream_error(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(errors.UpstreamError, match=match):
        profiler.parse_kernel_trace(_write_trace(tmp_path, text))


def test_summarize_sorts_by_total_time() -> None:
    kernels = [_stat("small", 1.0), _stat("big", 4.0), _stat("middle", 2.0)]
    assert [stat.name for stat in profiler.summarize(kernels)] == ["big", "middle", "small"]


def test_summarize_truncates_to_top() -> None:
    kernels = [_stat("a", 1.0), _stat("b", 2.0), _stat("c", 3.0)]
    assert [stat.name for stat in profiler.summarize(kernels, top=2)] == ["c", "b"]


def test_summarize_breaks_ties_on_name() -> None:
    kernels = [_stat("mul_kernel", 2.0), _stat("add_kernel", 2.0)]
    assert [stat.name for stat in profiler.summarize(kernels)] == [
        "add_kernel",
        "mul_kernel",
    ]


def test_kernel_stat_as_dict_is_json_friendly() -> None:
    payload = _stat("add_kernel", 1.5e-05).as_dict()
    assert set(payload) == {"name", "calls", "total_s", "mean_s", "p50_s", "share"}
    assert json.loads(json.dumps(payload))["name"] == "add_kernel"


def test_profile_summary_as_dict_holds_kernels_and_files() -> None:
    summary = profiler.ProfileSummary(
        total_s=1.5e-05,
        kernels=[_stat("add_kernel", 1.5e-05)],
        trace_files=["trace_kernel_trace.csv"],
    )
    payload = summary.as_dict()
    assert payload["trace_files"] == ["trace_kernel_trace.csv"]
    decoded = json.loads(json.dumps(payload))
    assert decoded["kernels"][0]["name"] == "add_kernel"
    assert decoded["total_s"] == pytest.approx(1.5e-05)


def test_rocprofv3_available_reports_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profiler.shutil, "which", lambda name: f"/opt/rocm/bin/{name}")
    assert profiler.rocprofv3_available() is True
    monkeypatch.setattr(profiler.shutil, "which", lambda name: None)
    assert profiler.rocprofv3_available() is False


def test_rocprofv3_available_is_a_bool_on_this_host() -> None:
    """The host either has ROCm or does not; the check only has to report it."""
    assert isinstance(profiler.rocprofv3_available(), bool)


def test_run_profiled_summarizes_the_trace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    _fake_profiler(monkeypatch, writes=_write_duration_trace, calls=calls)
    output_dir = tmp_path / "profile"
    summary = profiler.run_profiled(["true"], output_dir)
    assert output_dir.is_dir()
    assert summary.total_s == pytest.approx(DURATION_TOTAL_S)
    assert [stat.name for stat in summary.kernels] == ["mul_kernel", "add_kernel", "relu_kernel"]
    assert [Path(path).name for path in summary.trace_files] == ["trace_kernel_trace.csv"]
    command, options = calls[0]
    assert command == [
        "/opt/rocm/bin/rocprofv3",
        "--kernel-trace",
        "--output-format",
        "csv",
        "-o",
        str(output_dir / "trace"),
        "--",
        "true",
    ]
    assert options["capture_output"] is True
    assert options["timeout"] == profiler.DEFAULT_TIMEOUT_S


def test_run_profiled_passes_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    _fake_profiler(monkeypatch, writes=_write_duration_trace, calls=calls)
    profiler.run_profiled(["true"], tmp_path / "profile", timeout_s=42.0)
    assert calls[0][1]["timeout"] == 42.0


def test_run_profiled_merges_kernel_traces_and_skips_other_csvs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Supporting CSVs do not break the parse, and repeated kernels are pooled."""

    def writes(output_prefix: Path) -> None:
        output_dir = output_prefix.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "trace_agent_info.csv").write_text(
            '"Agent_Id","Agent_Type"\n"Agent 4","GPU"\n', encoding="utf-8"
        )
        (output_dir / "trace_kernel_trace.csv").write_text(
            '"Kernel_Name","Duration"\n"add_kernel",1000\n', encoding="utf-8"
        )
        (output_dir / "trace_kernel_trace_pass_1.csv").write_text(
            '"Kernel_Name","Duration"\n"add_kernel",3000\n"mul_kernel",2000\n',
            encoding="utf-8",
        )

    _fake_profiler(monkeypatch, writes=writes)
    summary = profiler.run_profiled(["true"], tmp_path / "profile")
    merged = _by_name(summary.kernels)
    assert merged["add_kernel"].calls == 2
    assert merged["add_kernel"].total_s == pytest.approx(4.0e-06)
    assert merged["mul_kernel"].calls == 1
    assert merged["mul_kernel"].total_s == pytest.approx(2.0e-06)
    assert summary.total_s == pytest.approx(6.0e-06)
    names = sorted(Path(path).name for path in summary.trace_files)
    assert names == ["trace_kernel_trace.csv", "trace_kernel_trace_pass_1.csv"]


def test_run_profiled_without_rocprofv3_raises_missing_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(profiler.shutil, "which", lambda name: None)

    def run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no process may be spawned without rocprofv3")

    monkeypatch.setattr(profiler.subprocess, "run", run)
    output_dir = tmp_path / "profile"
    with pytest.raises(errors.MissingDependency, match="PATH"):
        profiler.run_profiled(["true"], output_dir)
    assert not output_dir.exists()


def test_run_profiled_nonzero_exit_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_profiler(monkeypatch, returncode=2, stderr="rocprofv3: could not initialize")
    with pytest.raises(errors.UpstreamError, match="could not initialize"):
        profiler.run_profiled(["true"], tmp_path / "profile")


def test_run_profiled_without_csv_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_profiler(monkeypatch, stdout="no kernel dispatches were traced")
    with pytest.raises(errors.UpstreamError, match="no CSV.*no kernel dispatches"):
        profiler.run_profiled(["true"], tmp_path / "profile")


def test_run_profiled_timeout_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(profiler.shutil, "which", lambda name: "/opt/rocm/bin/rocprofv3")

    def run(command: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", profiler.DEFAULT_TIMEOUT_S))

    monkeypatch.setattr(profiler.subprocess, "run", run)
    with pytest.raises(errors.UpstreamError, match="did not finish"):
        profiler.run_profiled(["true"], tmp_path / "profile", timeout_s=1.0)


def test_run_profiled_oserror_raises_missing_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(profiler.shutil, "which", lambda name: "/opt/rocm/bin/rocprofv3")

    def run(command: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError("executable vanished")

    monkeypatch.setattr(profiler.subprocess, "run", run)
    with pytest.raises(errors.MissingDependency, match="cannot run"):
        profiler.run_profiled(["true"], tmp_path / "profile")
