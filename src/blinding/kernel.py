"""Run the HIP copy and GEMM benchmark and turn its JSON into report rows.

The C++ program in kernel/hip_bench.cpp measures the device. This module finds
that binary, runs it, and refuses to pass on a result that does not have the
promised shape. A measurement that reaches a run record is evidence, so a
missing binary, a non-zero exit, or malformed JSON raises instead of returning
a partial result.

The binary prints benchmark schema 2: every row carries one sample per timed
iteration, and a copy row names the variant that produced it. Schema 1 rows
have neither field, and they stay accepted so an older binary on PATH still
works.

No GPU work happens at import time. run_kernel is the only function that
spawns the benchmark, and it spawns exactly one process.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blinding.errors import MissingDependency, UpstreamError, UsageError

BINARY_NAME = "blinding-hip-bench"
BINARY_ENV = "BLINDING_HIP_BENCH"
DEFAULT_TIMEOUT_S = 600.0
STDERR_TAIL = 400
KNOWN_DTYPES = frozenset({"fp16", "fp32"})
KNOWN_LAYOUTS = frozenset({"nn", "nt", "tn"})
MIN_BLOCK_SIZE = 1
MAX_BLOCK_SIZE = 1024
MIN_PROBLEM_SIZE = 2
MAX_PROBLEM_SIZE = 16384
DEFAULT_COPY_VARIANTS = ("vec4-i1-w4",)
COPY_VARIANT_PATTERN = re.compile(r"vec[24]-i[124]-w[124]")
BENCH_SCHEMA = 2
SUPPORTED_BENCH_SCHEMAS = frozenset({1, 2})
PEAKS_PATH = Path(__file__).resolve().parents[2] / "configs" / "rocm-peaks.toml"


@dataclass(frozen=True, slots=True)
class KernelConfig:
    """Everything one benchmark run needs."""

    binary: str | None
    blocks: tuple[int, ...]
    sizes: tuple[int, ...]
    dtypes: tuple[str, ...]
    layouts: tuple[str, ...]
    timeout_s: float = DEFAULT_TIMEOUT_S
    copy_variants: tuple[str, ...] = DEFAULT_COPY_VARIANTS
    autotune_copy: bool = False


def _locate(candidate: str) -> str | None:
    """Resolve an explicit path or a bare name to an existing file."""
    expanded = Path(candidate).expanduser()
    if expanded.is_file():
        return str(expanded)
    return shutil.which(candidate)


def resolve_binary(config: KernelConfig) -> str:
    """Path to the benchmark binary: config first, then env, then PATH."""
    if config.binary:
        located = _locate(config.binary)
        if located is None:
            raise MissingDependency(
                f"HIP benchmark binary {config.binary!r} is not an existing file; build "
                "kernel/hip_bench.cpp with hipcc and pass its path"
            )
        return located
    configured = os.environ.get(BINARY_ENV, "").strip()
    if configured:
        located = _locate(configured)
        if located is None:
            raise MissingDependency(
                f"{BINARY_ENV} is set to {configured!r}, which is not an existing file"
            )
        return located
    found = shutil.which(BINARY_NAME)
    if found is None:
        raise MissingDependency(
            f"{BINARY_NAME} is not on PATH; build kernel/hip_bench.cpp with hipcc in the "
            f"ROCm image, then put the binary on PATH or set {BINARY_ENV}"
        )
    return found


def _validate_config(config: KernelConfig) -> None:
    """Reject a request the C++ program would reject, before spawning it."""
    if not config.blocks or not config.sizes or not config.dtypes or not config.layouts:
        raise UsageError("blocks, sizes, dtypes, and layouts must not be empty")
    if any(block < MIN_BLOCK_SIZE or block > MAX_BLOCK_SIZE for block in config.blocks):
        raise UsageError(f"block sizes must be between {MIN_BLOCK_SIZE} and {MAX_BLOCK_SIZE}")
    if any(size < MIN_PROBLEM_SIZE or size > MAX_PROBLEM_SIZE for size in config.sizes):
        raise UsageError(f"matrix sizes must be between {MIN_PROBLEM_SIZE} and {MAX_PROBLEM_SIZE}")
    unknown_dtypes = sorted(set(config.dtypes) - KNOWN_DTYPES)
    if unknown_dtypes:
        raise UsageError(f"unknown dtypes: {', '.join(unknown_dtypes)}")
    unknown_layouts = sorted(set(config.layouts) - KNOWN_LAYOUTS)
    if unknown_layouts:
        raise UsageError(f"unknown layouts: {', '.join(unknown_layouts)}")
    if not config.copy_variants:
        raise UsageError("copy_variants must not be empty")
    unknown_variants = [
        str(variant)
        for variant in config.copy_variants
        if not isinstance(variant, str) or not COPY_VARIANT_PATTERN.fullmatch(variant)
    ]
    if unknown_variants:
        raise UsageError(
            f"unknown copy variants: {', '.join(unknown_variants)}; expected vecN-iM-wW "
            "with N in 2 or 4, M in 1, 2, or 4, and W in 1, 2, or 4"
        )
    if config.timeout_s <= 0.0:
        raise UsageError("timeout_s must be positive")


def build_command(config: KernelConfig, binary: str) -> list[str]:
    """The argv for one benchmark run."""
    command = [
        binary,
        "--json",
        "--blocks",
        ",".join(str(block) for block in config.blocks),
        "--sizes",
        ",".join(str(size) for size in config.sizes),
        "--dtype",
        ",".join(config.dtypes),
        "--layouts",
        ",".join(config.layouts),
    ]
    if config.copy_variants != DEFAULT_COPY_VARIANTS:
        command.extend(["--copy-variants", ",".join(config.copy_variants)])
    if config.autotune_copy:
        command.append("--autotune-copy")
    return command


def _tail(text: str | None) -> str:
    """Last part of a stderr stream, or a placeholder when it is empty."""
    cleaned = (text or "").strip()
    if not cleaned:
        return "no stderr"
    return cleaned[-STDERR_TAIL:]


def _is_number(value: Any) -> bool:
    """True for int and float, but not for bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_positive_int(entry: dict[str, Any], key: str, source: str, where: str) -> int:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UpstreamError(
            f"{source} printed {where} field {key!r} that is not a positive integer"
        )
    return value


def _require_positive_number(entry: dict[str, Any], key: str, source: str, where: str) -> float:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0.0:
        raise UpstreamError(f"{source} printed {where} field {key!r} that is not a positive number")
    return float(value)


def _validate_samples(entry: dict[str, Any], schema: int, source: str, where: str) -> None:
    """Check the per-iteration samples a schema 2 row has to carry."""
    samples = entry.get("samples")
    if samples is None:
        if schema >= BENCH_SCHEMA:
            raise UpstreamError(f"{source} printed {where} without a 'samples' list")
        return
    if not isinstance(samples, list) or not samples:
        raise UpstreamError(
            f"{source} printed {where} field 'samples' that is not a non-empty list"
        )
    for value in samples:
        if not _is_number(value) or value <= 0.0 or not math.isfinite(float(value)):
            raise UpstreamError(
                f"{source} printed {where} with a 'samples' value that is not a positive number"
            )


def _validate_payload(payload: Any, source: str) -> dict[str, Any]:
    """Check the JSON contract and return the object unchanged."""
    if not isinstance(payload, dict):
        raise UpstreamError(f"{source} printed JSON that is not an object")
    data: dict[str, Any] = {str(key): value for key, value in payload.items()}

    schema = data.get("bench_schema", 1)
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema not in SUPPORTED_BENCH_SCHEMAS
    ):
        raise UpstreamError(f"{source} printed JSON field 'bench_schema' that is not 1 or 2")

    for key in ("device", "gfx", "rocm_version"):
        if not isinstance(data.get(key), str):
            raise UpstreamError(f"{source} printed JSON field {key!r} that is not a string")
    release = data.get("rocm_release")
    if release is not None and not isinstance(release, str):
        raise UpstreamError(f"{source} printed JSON field 'rocm_release' that is not a string")
    clocks = data.get("clocks")
    if clocks is not None and not isinstance(clocks, dict):
        raise UpstreamError(f"{source} printed JSON field 'clocks' that is not an object")
    errors = data.get("errors")
    if not isinstance(errors, list):
        raise UpstreamError(f"{source} printed JSON field 'errors' that is not a list")
    reported = [str(item) for item in errors]
    if reported:
        raise UpstreamError(f"{source} reported errors: {'; '.join(reported)}")

    copy_rows = data.get("copy")
    if not isinstance(copy_rows, list) or not copy_rows:
        raise UpstreamError(f"{source} printed JSON field 'copy' that is not a non-empty list")
    for entry in copy_rows:
        if not isinstance(entry, dict):
            raise UpstreamError(f"{source} printed a copy row that is not an object")
        _require_positive_int(entry, "block", source, "copy row")
        _require_positive_number(entry, "gbps", source, "copy row")
        variant = entry.get("variant")
        if variant is None:
            if schema >= BENCH_SCHEMA:
                raise UpstreamError(f"{source} printed a copy row without a 'variant' string")
        elif not isinstance(variant, str) or not variant:
            raise UpstreamError(
                f"{source} printed a copy row field 'variant' that is not a non-empty string"
            )
        _validate_samples(entry, schema, source, "copy row")

    gemm_rows = data.get("gemm")
    if not isinstance(gemm_rows, list) or not gemm_rows:
        raise UpstreamError(f"{source} printed JSON field 'gemm' that is not a non-empty list")
    for entry in gemm_rows:
        if not isinstance(entry, dict):
            raise UpstreamError(f"{source} printed a GEMM row that is not an object")
        dtype = entry.get("dtype")
        if not isinstance(dtype, str) or dtype not in KNOWN_DTYPES:
            raise UpstreamError(f"{source} printed a GEMM row with unknown dtype {dtype!r}")
        layout = entry.get("layout")
        if not isinstance(layout, str) or layout not in KNOWN_LAYOUTS:
            raise UpstreamError(f"{source} printed a GEMM row with unknown layout {layout!r}")
        _require_positive_int(entry, "size", source, "GEMM row")
        _require_positive_number(entry, "gflops", source, "GEMM row")
        _validate_samples(entry, schema, source, "GEMM row")

    return data


def run_kernel(config: KernelConfig) -> dict[str, Any]:
    """Run the HIP benchmark once and return its validated JSON object."""
    _validate_config(config)
    binary = resolve_binary(config)
    command = build_command(config, binary)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=config.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpstreamError(f"{binary} did not finish within {config.timeout_s:.0f}s") from exc
    except OSError as exc:
        raise MissingDependency(f"cannot run {binary}: {exc}") from exc
    if completed.returncode != 0:
        raise UpstreamError(f"{binary} exited {completed.returncode}: {_tail(completed.stderr)}")
    try:
        payload: Any = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise UpstreamError(f"{binary} printed malformed JSON: {exc}") from exc
    return _validate_payload(payload, binary)


def load_peaks(gfx: str) -> dict[str, Any] | None:
    """Peak table for one gfx architecture, or None when it is not configured.

    A wheel install does not carry the configs directory, so a missing file
    means no roofline rather than a failed run.
    """
    try:
        with PEAKS_PATH.open("rb") as stream:
            document = tomllib.load(stream)
    except OSError:
        return None
    table = document.get(gfx)
    if not isinstance(table, dict):
        return None
    return {str(key): value for key, value in table.items()}


def _percent_of_peak(value: Any, peak: Any) -> float | None:
    """Percent of a peak figure, or None when either side is not usable."""
    if isinstance(value, bool) or isinstance(peak, bool):
        return None
    if not isinstance(value, (int, float)) or not isinstance(peak, (int, float)):
        return None
    if float(peak) <= 0.0:
        return None
    return float(value) / float(peak) * 100.0


def _gflops_from_tflops(value: Any) -> float | None:
    """Convert a peak in TFLOPS to GFLOPS, matching the measured rows."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) * 1000.0


def attach_roofline(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a validated result carrying percent-of-peak values.

    Copy rows compare against the memory bandwidth and GEMM rows against the
    throughput for their dtype. A row keeps every field it already had. When
    the gfx has no peak table the object comes back unchanged.
    """
    gfx = data.get("gfx")
    peaks = load_peaks(gfx) if isinstance(gfx, str) else None
    if peaks is None:
        return data
    result: dict[str, Any] = copy.deepcopy(data)
    result["peaks"] = peaks
    bandwidth = peaks.get("bandwidth_gbps")
    for entry in result.get("copy", []):
        percent = _percent_of_peak(entry.get("gbps"), bandwidth)
        if percent is not None:
            entry["pct_of_peak"] = percent
    for entry in result.get("gemm", []):
        key = "fp16_tflops" if entry.get("dtype") == "fp16" else "fp32_tflops"
        throughput = _gflops_from_tflops(peaks.get(key))
        percent = _percent_of_peak(entry.get("gflops"), throughput)
        if percent is not None:
            entry["pct_of_peak"] = percent
    for entry in result.get("autotune", []):
        percent = _percent_of_peak(entry.get("gbps"), bandwidth)
        if percent is not None:
            entry["pct_of_peak"] = percent
    return result


def _rate_cell(value: Any, unit: str, entry: dict[str, Any]) -> str:
    """A rate with its unit, plus the percent of peak when one is attached."""
    text = f"{float(value):.1f} {unit}"
    percent = entry.get("pct_of_peak")
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        text += f" ({float(percent):.1f}% peak)"
    return text


def summary_rows(data: dict[str, Any]) -> list[list[str]]:
    """Rows for the kernel/detail/value table, copy rows first then GEMM rows.

    The caller passes the same headers the command line prints, so one row
    carries everything a reader needs: which benchmark ran, which point on it,
    and what came back. Units live in the value cell because the table has no
    per-column format.
    """
    rows: list[list[str]] = []
    for entry in data["copy"]:
        variant = entry.get("variant")
        benchmark = "copy" if not variant else f"copy {variant}"
        rows.append(
            [
                benchmark,
                f"block {entry['block']}",
                _rate_cell(entry["gbps"], "GB/s", entry),
            ]
        )
    for entry in data["gemm"]:
        rows.append(
            [
                f"gemm {entry['dtype']}",
                f"{entry['layout']} {entry['size']}",
                _rate_cell(entry["gflops"], "GFLOPS", entry),
            ]
        )
    return rows


def _percent_cell(entry: dict[str, Any]) -> str:
    """A percent-of-peak cell, or a dash when the row has no peak attached."""
    percent = entry.get("pct_of_peak")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return "-"
    return f"{float(percent):.1f}"


def render_text(data: dict[str, Any]) -> str:
    """Human-readable summary of one validated benchmark result."""
    gfx = data.get("gfx") or "unknown gfx"
    lines = [
        f"device    {data['device']} ({gfx})",
        f"rocm      {data['rocm_version']}",
    ]
    release = data.get("rocm_release")
    if isinstance(release, str) and release:
        lines.append(f"release   {release}")
    clocks = data.get("clocks")
    if isinstance(clocks, dict):
        readings = [f"{key}={value}" for key, value in sorted(clocks.items()) if value is not None]
        if readings:
            lines.append("clocks    " + " ".join(readings))
    peaks = data.get("peaks")
    if isinstance(peaks, dict) and isinstance(peaks.get("name"), str):
        lines.append(f"peaks     {peaks['name']}")
    lines.append("")
    lines.append("copy")
    lines.append(f"{'block':>6}  {'variant':<12}  {'GB/s':>9}  {'% peak':>6}")
    for entry in data["copy"]:
        variant = entry.get("variant")
        label = variant if isinstance(variant, str) and variant else "-"
        lines.append(
            f"{entry['block']:>6}  {label:<12}  {float(entry['gbps']):>9.1f}  "
            f"{_percent_cell(entry):>6}"
        )
    lines.append("")
    lines.append("gemm")
    lines.append(f"{'dtype':<6}  {'layout':<6}  {'size':>6}  {'GFLOPS':>10}  {'% peak':>6}")
    for entry in data["gemm"]:
        lines.append(
            f"{entry['dtype']:<6}  {entry['layout']:<6}  {int(entry['size']):>6}  "
            f"{float(entry['gflops']):>10.1f}  {_percent_cell(entry):>6}"
        )
    return "\n".join(lines)
