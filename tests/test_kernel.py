"""Tests for the HIP benchmark wrapper.

The lab's GPU is not available to this test suite, so every case drives the
module through a fake subprocess. One fixture holds a well-formed result; the
invalid cases mutate a single field of it so a failure names the rule it broke.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from blinding import errors, kernel

FIXTURE_JSON = """
{
  "device": "AMD Radeon RX 7800 XT",
  "gfx": "gfx1101",
  "rocm_version": "6.2.41133",
  "rocm_release": "6.2.0",
  "copy": [
    {"block": 128, "gbps": 142.5},
    {"block": 256, "gbps": 151.3}
  ],
  "gemm": [
    {"dtype": "fp32", "layout": "nn", "size": 1024, "gflops": 4321.5},
    {"dtype": "fp16", "layout": "nt", "size": 2048, "gflops": 12345.6}
  ],
  "clocks": {"gpu_mhz": null, "power_w": null},
  "errors": []
}
"""

# Each mapping is applied on top of the fixture. Every field is invalid on its
# own, so one failing case cannot hide behind another.
INVALID_PAYLOADS: list[dict[str, Any]] = [
    {"copy": [{"block": "128", "gbps": 142.5}]},
    {"copy": [{"block": 128, "gbps": True}]},
    {"copy": [{"block": 128, "gbps": 0.0}]},
    {"gemm": [{"dtype": "bf16", "layout": "nn", "size": 1024, "gflops": 1.0}]},
    {"gemm": [{"dtype": "fp32", "layout": "nope", "size": 1024, "gflops": 1.0}]},
    {"gemm": [{"dtype": "fp32", "layout": "nn", "size": 1024, "gflops": "fast"}]},
    {"device": 7},
    {"errors": "none"},
    {"clocks": []},
]

INVALID_CONFIGS: list[dict[str, Any]] = [
    {"blocks": ()},
    {"sizes": (0,)},
    {"dtypes": ("bf16",)},
    {"layouts": ("tt",)},
    {"timeout_s": 0.0},
]


@pytest.fixture()
def fake_binary(tmp_path: Path) -> str:
    """An executable stand-in for the HIP benchmark.

    resolve_binary insists on an existing file, so a bare fake path would fail
    before the subprocess fake ever ran.
    """
    path = tmp_path / "blinding-hip-bench"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _config(binary: str | None, **overrides: Any) -> kernel.KernelConfig:
    values: dict[str, Any] = {
        "binary": binary,
        "blocks": (128, 256),
        "sizes": (1024, 2048),
        "dtypes": ("fp16", "fp32"),
        "layouts": ("nn", "nt", "tn"),
    }
    values.update(overrides)
    return kernel.KernelConfig(**values)


def _payload(**overrides: Any) -> str:
    payload = json.loads(FIXTURE_JSON)
    payload.update(overrides)
    return json.dumps(payload)


def _fake_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = FIXTURE_JSON,
    stderr: str = "",
    calls: list[list[str]] | None = None,
) -> None:
    """Replace the subprocess call the module makes with a canned result."""

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(kernel.subprocess, "run", run)


def test_run_kernel_returns_fixture_object(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    _fake_run(monkeypatch)
    result = kernel.run_kernel(_config(fake_binary))
    assert result == json.loads(FIXTURE_JSON)
    assert result["device"] == "AMD Radeon RX 7800 XT"


def test_build_command_contains_requested_values(fake_binary: str) -> None:
    config = _config(fake_binary, blocks=(128, 256), sizes=(1024, 2048))
    command = kernel.build_command(config, "/opt/bench")
    assert command == [
        "/opt/bench",
        "--json",
        "--blocks",
        "128,256",
        "--sizes",
        "1024,2048",
        "--dtype",
        "fp16,fp32",
        "--layouts",
        "nn,nt,tn",
    ]


def test_run_kernel_sends_requested_blocks_and_sizes(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    calls: list[list[str]] = []
    _fake_run(monkeypatch, calls=calls)
    config = _config(
        fake_binary,
        blocks=(64, 1024),
        sizes=(1024, 4096),
        dtypes=("fp32",),
        layouts=("nn",),
    )
    kernel.run_kernel(config)
    assert len(calls) == 1
    command = calls[0]
    assert command[command.index("--blocks") + 1] == "64,1024"
    assert command[command.index("--sizes") + 1] == "1024,4096"
    assert command[command.index("--dtype") + 1] == "fp32"
    assert command[command.index("--layouts") + 1] == "nn"


def test_run_kernel_passes_timeout(monkeypatch: pytest.MonkeyPatch, fake_binary: str) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, FIXTURE_JSON, "")

    monkeypatch.setattr(kernel.subprocess, "run", run)
    kernel.run_kernel(_config(fake_binary, timeout_s=42.0))
    assert seen["timeout"] == 42.0


def test_summary_rows_cover_copy_and_gemm() -> None:
    rows = kernel.summary_rows(json.loads(FIXTURE_JSON))
    assert rows == [
        ["copy", "block 128", "142.5 GB/s"],
        ["copy", "block 256", "151.3 GB/s"],
        ["gemm fp32", "nn 1024", "4321.5 GFLOPS"],
        ["gemm fp16", "nt 2048", "12345.6 GFLOPS"],
    ]


def test_render_text_shows_device_and_tables() -> None:
    text = kernel.render_text(json.loads(FIXTURE_JSON))
    assert "AMD Radeon RX 7800 XT" in text
    assert "gfx1101" in text
    assert "6.2.41133" in text
    assert "6.2.0" in text
    assert "copy" in text
    assert "gemm" in text
    assert "142.5" in text
    assert "12345.6" in text


def test_render_text_shows_clock_readings_when_present() -> None:
    data = json.loads(FIXTURE_JSON)
    data["clocks"] = {"gpu_mhz": 2400.0, "power_w": 250.0}
    text = kernel.render_text(data)
    assert "gpu_mhz=2400.0" in text
    assert "power_w=250.0" in text


def test_resolve_binary_accepts_existing_file(fake_binary: str) -> None:
    assert kernel.resolve_binary(_config(fake_binary)) == fake_binary


def test_resolve_binary_prefers_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configured = tmp_path / "configured-bench"
    configured.write_text("", encoding="utf-8")
    monkeypatch.setenv(kernel.BINARY_ENV, str(tmp_path / "from-env"))
    assert kernel.resolve_binary(_config(str(configured))) == str(configured)


def test_resolve_binary_uses_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "blinding-hip-bench"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setenv(kernel.BINARY_ENV, str(binary))
    assert kernel.resolve_binary(_config(None)) == str(binary)


def test_resolve_binary_falls_back_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(kernel.BINARY_ENV, raising=False)
    monkeypatch.setattr(kernel.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert kernel.resolve_binary(_config(None)) == "/usr/local/bin/blinding-hip-bench"


def test_missing_binary_raises_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(kernel.BINARY_ENV, raising=False)
    monkeypatch.setattr(kernel.shutil, "which", lambda name: None)

    def run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no process may be spawned without a binary")

    monkeypatch.setattr(kernel.subprocess, "run", run)
    with pytest.raises(errors.MissingDependency):
        kernel.run_kernel(_config(None))


def test_missing_configured_binary_raises_missing_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(kernel.shutil, "which", lambda name: None)
    with pytest.raises(errors.MissingDependency):
        kernel.resolve_binary(_config(str(tmp_path / "absent")))


def test_nonzero_exit_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    _fake_run(
        monkeypatch,
        returncode=1,
        stdout="",
        stderr="HIP error in hipMalloc: out of memory",
    )
    with pytest.raises(errors.UpstreamError, match="hipMalloc"):
        kernel.run_kernel(_config(fake_binary))


def test_timeout_raises_upstream_error(monkeypatch: pytest.MonkeyPatch, fake_binary: str) -> None:
    def run(command: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 600.0))

    monkeypatch.setattr(kernel.subprocess, "run", run)
    with pytest.raises(errors.UpstreamError, match="did not finish"):
        kernel.run_kernel(_config(fake_binary, timeout_s=1.0))


def test_malformed_json_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    _fake_run(monkeypatch, stdout='{"device":')
    with pytest.raises(errors.UpstreamError, match="malformed JSON"):
        kernel.run_kernel(_config(fake_binary))


def test_non_object_json_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    _fake_run(monkeypatch, stdout="[]")
    with pytest.raises(errors.UpstreamError, match="not an object"):
        kernel.run_kernel(_config(fake_binary))


@pytest.mark.parametrize("field", ["copy", "gemm"])
def test_empty_result_list_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str, field: str
) -> None:
    _fake_run(monkeypatch, stdout=_payload(**{field: []}))
    with pytest.raises(errors.UpstreamError):
        kernel.run_kernel(_config(fake_binary))


@pytest.mark.parametrize("overrides", INVALID_PAYLOADS)
def test_invalid_payload_field_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str, overrides: dict[str, Any]
) -> None:
    _fake_run(monkeypatch, stdout=_payload(**overrides))
    with pytest.raises(errors.UpstreamError):
        kernel.run_kernel(_config(fake_binary))


def test_reported_errors_raise_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    _fake_run(monkeypatch, stdout=_payload(errors=["rocBLAS error: internal error"]))
    with pytest.raises(errors.UpstreamError, match="internal error"):
        kernel.run_kernel(_config(fake_binary))


@pytest.mark.parametrize("overrides", INVALID_CONFIGS)
def test_invalid_config_raises_usage_error(fake_binary: str, overrides: dict[str, Any]) -> None:
    with pytest.raises(errors.UsageError):
        kernel.run_kernel(_config(fake_binary, **overrides))
