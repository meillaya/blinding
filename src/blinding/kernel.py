"""Run the HIP copy and GEMM benchmark and turn its JSON into report rows.

The C++ program in kernel/hip_bench.cpp measures the device. This module finds
that binary, runs it, and refuses to pass on a result that does not have the
promised shape. A measurement that reaches a run record is evidence, so a
missing binary, a non-zero exit, or malformed JSON raises instead of returning
a partial result.

No GPU work happens at import time. run_kernel is the only function that
spawns the benchmark, and it spawns exactly one process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


@dataclass(frozen=True, slots=True)
class KernelConfig:
    """Everything one benchmark run needs."""

    binary: str | None
    blocks: tuple[int, ...]
    sizes: tuple[int, ...]
    dtypes: tuple[str, ...]
    layouts: tuple[str, ...]
    timeout_s: float = DEFAULT_TIMEOUT_S


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
    if config.timeout_s <= 0.0:
        raise UsageError("timeout_s must be positive")


def build_command(config: KernelConfig, binary: str) -> list[str]:
    """The argv for one benchmark run."""
    return [
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


def _validate_payload(payload: Any, source: str) -> dict[str, Any]:
    """Check the JSON contract and return the object unchanged."""
    if not isinstance(payload, dict):
        raise UpstreamError(f"{source} printed JSON that is not an object")
    data: dict[str, Any] = {str(key): value for key, value in payload.items()}

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

    copy = data.get("copy")
    if not isinstance(copy, list) or not copy:
        raise UpstreamError(f"{source} printed JSON field 'copy' that is not a non-empty list")
    for entry in copy:
        if not isinstance(entry, dict):
            raise UpstreamError(f"{source} printed a copy row that is not an object")
        _require_positive_int(entry, "block", source, "copy row")
        _require_positive_number(entry, "gbps", source, "copy row")

    gemm = data.get("gemm")
    if not isinstance(gemm, list) or not gemm:
        raise UpstreamError(f"{source} printed JSON field 'gemm' that is not a non-empty list")
    for entry in gemm:
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


def summary_rows(data: dict[str, Any]) -> list[list[str]]:
    """Rows for the kernel/detail/value table, copy rows first then GEMM rows.

    The caller passes the same headers the command line prints, so one row
    carries everything a reader needs: which benchmark ran, which point on it,
    and what came back. Units live in the value cell because the table has no
    per-column format.
    """
    rows: list[list[str]] = []
    for entry in data["copy"]:
        rows.append(["copy", f"block {entry['block']}", f"{float(entry['gbps']):.1f} GB/s"])
    for entry in data["gemm"]:
        rows.append(
            [
                f"gemm {entry['dtype']}",
                f"{entry['layout']} {entry['size']}",
                f"{float(entry['gflops']):.1f} GFLOPS",
            ]
        )
    return rows


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
    lines.append("")
    lines.append("copy")
    lines.append(f"{'block':>6}  {'GB/s':>9}")
    for entry in data["copy"]:
        lines.append(f"{entry['block']:>6}  {float(entry['gbps']):>9.1f}")
    lines.append("")
    lines.append("gemm")
    lines.append(f"{'dtype':<6}  {'layout':<6}  {'size':>6}  {'GFLOPS':>10}")
    for entry in data["gemm"]:
        lines.append(
            f"{entry['dtype']:<6}  {entry['layout']:<6}  {int(entry['size']):>6}  "
            f"{float(entry['gflops']):>10.1f}"
        )
    return "\n".join(lines)
