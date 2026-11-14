"""Parse rocprofv3 kernel traces and say where the GPU time went.

The profile commands run a child process under rocprofv3, read the kernel trace
CSV files it writes, and group the dispatches by kernel name. A run record that
carries kernel totals is evidence of what ran on the device, so a trace without
a name column or without duration data raises instead of returning an empty
table.

The parser stays pure and standard-library only: `parse_kernel_trace` takes a
path and returns statistics, while `run_profiled` is the one function that
spawns a process.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from blinding.errors import MissingDependency, UpstreamError
from blinding.stats import percentile

PROFILER_NAME = "rocprofv3"
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_TOP = 30
OUTPUT_TAIL = 400
NS_PER_S = 1.0e9
US_PER_S = 1.0e6
NAME_COLUMNS = ("Kernel_Name", "KernelName", "Name")
DURATION_COLUMNS = ("Duration", "Kernel_Duration", "Duration_Ns", "Duration_Us")
UNIT_SCALES = {"ns": 1.0 / NS_PER_S, "us": 1.0 / US_PER_S}


@dataclass(frozen=True, slots=True)
class KernelStat:
    """Time one kernel name consumed in a trace."""

    name: str
    calls: int
    total_s: float
    mean_s: float
    p50_s: float
    share: float

    def as_dict(self) -> dict[str, str | int | float]:
        """Plain mapping for the run record."""
        return {
            "name": self.name,
            "calls": self.calls,
            "total_s": self.total_s,
            "mean_s": self.mean_s,
            "p50_s": self.p50_s,
            "share": self.share,
        }


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    """Kernel totals for one profiled command and the traces behind them."""

    total_s: float
    kernels: list[KernelStat]
    trace_files: list[str]

    def as_dict(self) -> dict[str, object]:
        """Plain mapping for the run record."""
        return {
            "total_s": self.total_s,
            "kernels": [stat.as_dict() for stat in self.kernels],
            "trace_files": list(self.trace_files),
        }


def rocprofv3_available() -> bool:
    """True when a rocprofv3 binary is on PATH."""
    return shutil.which(PROFILER_NAME) is not None


def _normalized_columns(header: Sequence[str]) -> dict[str, str]:
    """Map each column's normalized name to the spelling the file uses."""
    return {column.strip().lower(): column for column in header}


def _find_column(columns: dict[str, str], aliases: Sequence[str]) -> str | None:
    """First alias present in the header, in the order the aliases are listed."""
    for alias in aliases:
        found = columns.get(alias.strip().lower())
        if found is not None:
            return found
    return None


def _column_unit(column: str) -> str:
    """Time unit encoded in a column name, nanoseconds unless the name says us."""
    return "us" if column.strip().lower().endswith("_us") else "ns"


def _timestamp_column(columns: dict[str, str], prefix: str) -> tuple[str, str] | None:
    """Timestamp column starting with `prefix`, with the unit its suffix states."""
    for normalized, original in columns.items():
        if not normalized.startswith(prefix):
            continue
        suffix = normalized[len(prefix) :]
        if not suffix:
            return original, "ns"
        if suffix in ("_ns", "_us"):
            return original, suffix[1:]
    return None


def _explicit_duration(columns: dict[str, str]) -> tuple[str, str] | None:
    """The duration column and its unit, when the trace reports one directly."""
    column = _find_column(columns, DURATION_COLUMNS)
    if column is None:
        return None
    return column, _column_unit(column)


def _timestamp_pair(columns: dict[str, str], source: str) -> tuple[str, str, str] | None:
    """The start and end timestamp columns and their shared unit."""
    start = _timestamp_column(columns, "start_timestamp")
    end = _timestamp_column(columns, "end_timestamp")
    if start is None or end is None:
        return None
    if start[1] != end[1]:
        raise UpstreamError(f"{source}: the start and end timestamp columns use different units")
    return start[0], end[0], start[1]


def _number(text: str | None, source: str, line: int, column: str) -> float:
    """One numeric cell, refusing an empty or malformed value."""
    raw = (text or "").strip()
    if not raw:
        raise UpstreamError(f"{source}: line {line} has no value in {column!r}")
    try:
        return float(raw)
    except ValueError:
        raise UpstreamError(
            f"{source}: line {line} has a non-numeric {column!r} value {raw!r}"
        ) from None


def _row_seconds(
    row: dict[str, str | None],
    line: int,
    source: str,
    explicit: tuple[str, str] | None,
    stamps: tuple[str, str, str] | None,
) -> float:
    """One row's kernel duration in seconds, from whichever source it carries."""
    if explicit is not None:
        column, unit = explicit
        return _number(row.get(column), source, line, column) * UNIT_SCALES[unit]
    if stamps is None:
        raise UpstreamError(f"{source}: no kernel duration data")
    start_column, end_column, unit = stamps
    start = _number(row.get(start_column), source, line, start_column)
    end = _number(row.get(end_column), source, line, end_column)
    if end < start:
        raise UpstreamError(f"{source}: line {line} ends before it starts")
    return (end - start) * UNIT_SCALES[unit]


def _duration_groups(path: Path) -> dict[str, list[float]]:
    """Group one trace's kernel dispatches by name, with durations in seconds."""
    source = str(path)
    try:
        handle = path.open(newline="", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise UpstreamError(f"cannot read trace {source}: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        if header is None:
            raise UpstreamError(f"{source}: the trace is empty")
        columns = _normalized_columns(header)
        name_column = _find_column(columns, NAME_COLUMNS)
        if name_column is None:
            expected = ", ".join(NAME_COLUMNS)
            raise UpstreamError(f"{source}: no kernel name column; expected one of {expected}")
        explicit = _explicit_duration(columns)
        stamps = _timestamp_pair(columns, source) if explicit is None else None
        if explicit is None and stamps is None:
            expected = ", ".join(DURATION_COLUMNS)
            raise UpstreamError(
                f"{source}: no kernel duration data; expected one of {expected} "
                "or Start_Timestamp/End_Timestamp"
            )
        groups: dict[str, list[float]] = {}
        for line, row in enumerate(reader, start=2):
            name = (row.get(name_column) or "").strip()
            if not name:
                continue
            groups.setdefault(name, []).append(_row_seconds(row, line, source, explicit, stamps))
    if not groups:
        raise UpstreamError(
            f"{source}: no kernel duration data rows; expected dispatches with a name"
        )
    return groups


def _kernel_stats(groups: dict[str, list[float]], source: str) -> list[KernelStat]:
    """Turn grouped durations into stats sorted by total time, longest first."""
    total_s = sum(sum(values) for values in groups.values())
    if total_s <= 0.0:
        raise UpstreamError(f"{source}: kernel durations add up to zero")
    stats: list[KernelStat] = []
    for name, values in groups.items():
        total = sum(values)
        stats.append(
            KernelStat(
                name=name,
                calls=len(values),
                total_s=total,
                mean_s=total / len(values),
                p50_s=percentile(values, 50.0),
                share=total / total_s,
            )
        )
    return sorted(stats, key=lambda stat: (-stat.total_s, stat.name))


def parse_kernel_trace(path: Path) -> list[KernelStat]:
    """Kernel statistics from one rocprofv3 kernel trace CSV.

    The header may name the kernel through `Kernel_Name`, `KernelName`, or
    `Name`, and may report the duration directly through `Duration`,
    `Kernel_Duration`, `Duration_Ns`, or `Duration_Us`. Without a duration
    column the duration is the gap between `End_Timestamp` and
    `Start_Timestamp`. A name suffix of `ns` or `us` sets the unit, and
    nanoseconds is the default.

    Rows without a kernel name are skipped. A file without a name column or
    without any duration data raises `UpstreamError`, because an empty profile
    is not evidence.
    """
    return _kernel_stats(_duration_groups(path), str(path))


def summarize(kernels: Sequence[KernelStat], top: int = DEFAULT_TOP) -> list[KernelStat]:
    """The costliest kernels, `top` of them, sorted by total device time.

    The share stays a fraction of all traced kernel time, so a truncated list
    sums to less than one.
    """
    ranked = sorted(kernels, key=lambda stat: (-stat.total_s, stat.name))
    return ranked[: max(0, top)]


def _profiled_command(executable: str, argv: Sequence[str], output_prefix: Path) -> list[str]:
    """The rocprofv3 command for one profiled child process."""
    return [
        executable,
        "--kernel-trace",
        "--output-format",
        "csv",
        "-o",
        str(output_prefix),
        "--",
        *argv,
    ]


def _output_tail(completed: subprocess.CompletedProcess[str]) -> str:
    """Short tail of what the tool printed, stderr first."""
    text = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    return text[-OUTPUT_TAIL:] if text else "no output"


def _trace_csvs(output_dir: Path) -> list[Path]:
    """CSV files under one output directory, kernel traces preferred.

    rocprofv3 also writes supporting files such as an agent info CSV. When a
    kernel trace is present, only kernel traces are read.
    """
    found = sorted(path for path in output_dir.rglob("*.csv") if path.is_file())
    kernel_traces = [path for path in found if "kernel_trace" in path.name.lower()]
    return kernel_traces or found


def run_profiled(
    argv: list[str],
    output_dir: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ProfileSummary:
    """Run one command under rocprofv3 and summarize the kernels it launched.

    `output_dir` is created if needed and receives the trace files. A missing
    rocprofv3 raises `MissingDependency`; a non-zero exit, a timeout, or an
    output directory without a CSV raises `UpstreamError` carrying a short tail
    of what the tool printed.
    """
    executable = shutil.which(PROFILER_NAME)
    if executable is None:
        raise MissingDependency(
            f"{PROFILER_NAME} is not on PATH; install ROCm and put {PROFILER_NAME} on PATH"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    command = _profiled_command(executable, argv, output_dir / "trace")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpstreamError(f"{PROFILER_NAME} did not finish within {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise MissingDependency(f"cannot run {PROFILER_NAME}: {exc}") from exc
    if completed.returncode != 0:
        raise UpstreamError(
            f"{PROFILER_NAME} exited {completed.returncode}: {_output_tail(completed)}"
        )
    trace_files = _trace_csvs(output_dir)
    if not trace_files:
        raise UpstreamError(
            f"{PROFILER_NAME} wrote no CSV for {output_dir}: {_output_tail(completed)}"
        )
    groups: dict[str, list[float]] = {}
    for path in trace_files:
        for name, values in _duration_groups(path).items():
            groups.setdefault(name, []).extend(values)
    kernels = _kernel_stats(groups, str(output_dir))
    return ProfileSummary(
        total_s=sum(stat.total_s for stat in kernels),
        kernels=kernels,
        trace_files=[str(path) for path in trace_files],
    )
