"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture
def fake_server() -> Path:
    """Path to the fake llama-server executable."""
    return FAKES / "fake_llama_server.py"


@pytest.fixture
def repo_root() -> Path:
    """The checkout root."""
    return ROOT


def write_bench_run(
    root: Path,
    name: str,
    *,
    ttfts: dict[int, list[float]],
    itls: dict[int, list[list[float]]] | None = None,
    decode: dict[int, float] | None = None,
    error_rate: dict[int, float] | None = None,
    valid: bool = True,
    host: dict[str, Any] | None = None,
    devices: list[dict[str, Any]] | None = None,
    command: str = "bench",
    blinding_version: str = "0.2.0",
    schema_version: int = 2,
    write_requests: bool = True,
) -> Path:
    """Create a synthetic run directory with a record and request samples."""
    run_dir = root / name
    run_dir.mkdir(parents=True)
    levels: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for level in sorted(ttfts):
        values = ttfts[level]
        groups = (itls or {}).get(level) or [[10.0] * 8 for _ in values]
        levels.append(
            {
                "concurrency": level,
                "decode_tokens_s": (decode or {}).get(level, 60.0),
                "error_rate": (error_rate or {}).get(level, 0.0),
                "ok": len(values),
                "requests": len(values),
            }
        )
        for ttft, group in zip(values, groups, strict=True):
            rows.append(
                {
                    "ok": "True",
                    "status": "200",
                    "ttft_ms": f"{ttft}",
                    "itl_ms": json.dumps(group),
                    "latency_ms": f"{ttft + len(group) * 10.0}",
                    "completion_tokens": str(len(group) + 1),
                    "prompt_tokens": "16",
                    "chunks": str(len(group) + 1),
                    "error": "",
                    "concurrency": str(level),
                }
            )
    record = {
        "schema_version": schema_version,
        "blinding_version": blinding_version,
        "command": command,
        "started_at": "2026-11-14T12:00:00Z",
        "argv": ["blinding", command],
        "git": {"commit": "a" * 40, "dirty": False},
        "host": host or {"hostname": "test-host", "cpu_model": "test-cpu"},
        "devices": devices if devices is not None else [],
        "model": {"id": "tiny", "quant": "q4"},
        "config": {"name": "test"},
        "tools": {"versions": {}},
        "validity": {"valid": valid, "reasons": [] if valid else ["too many errors"]},
        "metrics": {"levels": levels},
    }
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    if write_requests:
        with (run_dir / "requests.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return run_dir


@pytest.fixture
def bench_run_factory() -> Callable[..., Path]:
    """A callable that writes a synthetic bench run, for compare tests."""
    return write_bench_run


def write_kernel_run(
    root: Path,
    name: str,
    *,
    copy: list[dict[str, Any]],
    gemm: list[dict[str, Any]] | None = None,
    valid: bool = True,
    host: dict[str, Any] | None = None,
    devices: list[dict[str, Any]] | None = None,
    blinding_version: str = "0.3.0",
    command: str = "kernel",
) -> Path:
    """Create a synthetic kernel run directory with samples on each row."""
    run_dir = root / name
    run_dir.mkdir(parents=True)
    record = {
        "schema_version": 2,
        "blinding_version": blinding_version,
        "command": command,
        "started_at": "2026-11-14T12:00:00Z",
        "argv": ["blinding", "kernel"],
        "git": {"commit": "a" * 40, "dirty": False},
        "host": host or {"hostname": "test-host", "cpu_model": "test-cpu"},
        "devices": devices
        if devices is not None
        else [{"backend": "ROCm0", "name": "RX 7800 XT", "gfx": "gfx1101"}],
        "model": None,
        "config": {"blocks": [128, 256], "sizes": [1024]},
        "tools": {"versions": {}},
        "validity": {"valid": valid, "reasons": [] if valid else ["device error"]},
        "metrics": {"copy": copy, "gemm": gemm or []},
    }
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return run_dir


@pytest.fixture
def kernel_run_factory() -> Callable[..., Path]:
    """A callable that writes a synthetic kernel run, for compare tests."""
    return write_kernel_run
