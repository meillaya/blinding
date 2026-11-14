"""Tests for the HIP benchmark wrapper.

The lab's GPU is not available to this test suite, so every case drives the
module through a fake subprocess. One fixture holds a well-formed result; the
invalid cases mutate a single field of it so a failure names the rule it broke.
A second fixture keeps the schema 1 shape so an older binary on PATH stays
covered.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from blinding import errors, kernel

SCHEMA_2_JSON = """
{
  "bench_schema": 2,
  "device": "AMD Radeon RX 7800 XT",
  "gfx": "gfx1101",
  "rocm_version": "6.2.41133",
  "rocm_release": "6.2.0",
  "copy": [
    {"block": 128, "variant": "vec4-i1-w4", "gbps": 142.5,
     "samples": [141.0, 142.0, 143.0, 142.5, 141.5, 143.5, 142.0]},
    {"block": 256, "variant": "vec4-i1-w4", "gbps": 151.3,
     "samples": [150.0, 151.0, 152.0, 151.5, 150.5, 152.5, 151.0]}
  ],
  "gemm": [
    {"dtype": "fp32", "layout": "nn", "size": 1024, "gflops": 4321.5,
     "samples": [4300.0, 4320.0, 4340.0, 4321.5, 4310.0, 4330.0, 4325.0]},
    {"dtype": "fp16", "layout": "nt", "size": 2048, "gflops": 12345.6,
     "samples": [12300.0, 12340.0, 12350.0, 12345.6, 12320.0, 12360.0, 12345.0]}
  ],
  "clocks": {"gpu_mhz": null, "power_w": null},
  "errors": []
}
"""

LEGACY_JSON = """
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
    {"copy": [{"block": "128", "variant": "vec4-i1-w4", "gbps": 142.5, "samples": [1.0]}]},
    {"copy": [{"block": 128, "variant": "vec4-i1-w4", "gbps": True, "samples": [1.0]}]},
    {"copy": [{"block": 128, "variant": "vec4-i1-w4", "gbps": 0.0, "samples": [1.0]}]},
    {"copy": [{"block": 128, "variant": "vec4-i1-w4", "gbps": 142.5}]},
    {"copy": [{"block": 128, "gbps": 142.5, "samples": [1.0]}]},
    {"copy": [{"block": 128, "variant": 4, "gbps": 142.5, "samples": [1.0]}]},
    {"copy": [{"block": 128, "variant": "", "gbps": 142.5, "samples": [1.0]}]},
    {"copy": [{"block": 128, "variant": "vec4-i1-w4", "gbps": 142.5, "samples": []}]},
    {"copy": [{"block": 128, "variant": "vec4-i1-w4", "gbps": 142.5, "samples": "fast"}]},
    {"copy": [{"block": 128, "variant": "vec4-i1-w4", "gbps": 142.5, "samples": [0.0]}]},
    {"copy": [{"block": 128, "variant": "vec4-i1-w4", "gbps": 142.5, "samples": [True]}]},
    {"copy": [{"block": 128, "variant": "vec4-i1-w4", "gbps": 142.5, "samples": ["fast"]}]},
    {"gemm": [{"dtype": "bf16", "layout": "nn", "size": 1024, "gflops": 1.0, "samples": [1.0]}]},
    {"gemm": [{"dtype": "fp32", "layout": "nope", "size": 1024, "gflops": 1.0, "samples": [1.0]}]},
    {"gemm": [{"dtype": "fp32", "layout": "nn", "size": 1024, "gflops": "fast", "samples": [1.0]}]},
    {"gemm": [{"dtype": "fp32", "layout": "nn", "size": 1024, "gflops": 1.0}]},
    {"gemm": [{"dtype": "fp32", "layout": "nn", "size": 1024, "gflops": 1.0, "samples": [0.0]}]},
    {"bench_schema": 3},
    {"bench_schema": True},
    {"bench_schema": "2"},
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
    {"copy_variants": ()},
    {"copy_variants": ("vec3-i1-w4",)},
    {"copy_variants": ("vec4-i3-w4",)},
    {"copy_variants": ("vec4-i1-w8",)},
    {"copy_variants": ("nonsense",)},
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


def _payload(overrides: dict[str, Any] | None = None, source: str = SCHEMA_2_JSON) -> str:
    payload = json.loads(source)
    if overrides:
        payload.update(overrides)
    return json.dumps(payload)


def _fake_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = SCHEMA_2_JSON,
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
    assert result == json.loads(SCHEMA_2_JSON)
    assert result["device"] == "AMD Radeon RX 7800 XT"
    assert result["bench_schema"] == 2


def test_run_kernel_accepts_legacy_schema(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    _fake_run(monkeypatch, stdout=LEGACY_JSON)
    result = kernel.run_kernel(_config(fake_binary))
    assert result == json.loads(LEGACY_JSON)


def test_run_kernel_accepts_explicit_schema_one(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    payload = json.loads(LEGACY_JSON)
    payload["bench_schema"] = 1
    _fake_run(monkeypatch, stdout=json.dumps(payload))
    assert kernel.run_kernel(_config(fake_binary)) == payload


def test_legacy_row_samples_are_checked_when_present(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    payload = json.loads(LEGACY_JSON)
    payload["copy"][0]["samples"] = [1.0, 0.0]
    _fake_run(monkeypatch, stdout=json.dumps(payload))
    with pytest.raises(errors.UpstreamError, match="samples"):
        kernel.run_kernel(_config(fake_binary))


def test_autotune_rows_are_carried_through(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    payload = json.loads(SCHEMA_2_JSON)
    payload["autotune"] = [{"block": 128, "variant": "vec2-i1-w4", "gbps": 150.0}]
    _fake_run(monkeypatch, stdout=json.dumps(payload))
    result = kernel.run_kernel(_config(fake_binary))
    assert result["autotune"] == [{"block": 128, "variant": "vec2-i1-w4", "gbps": 150.0}]


def test_build_command_default_omits_new_flags(fake_binary: str) -> None:
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


def test_build_command_passes_copy_variants_when_non_default(fake_binary: str) -> None:
    config = _config(fake_binary, copy_variants=("vec2-i2-w2", "vec4-i1-w4"))
    command = kernel.build_command(config, "/opt/bench")
    assert command[command.index("--copy-variants") + 1] == "vec2-i2-w2,vec4-i1-w4"


def test_build_command_passes_autotune_flag(fake_binary: str) -> None:
    config = _config(fake_binary, autotune_copy=True)
    assert kernel.build_command(config, "/opt/bench")[-1] == "--autotune-copy"


def test_build_command_passes_both_new_flags(fake_binary: str) -> None:
    config = _config(fake_binary, copy_variants=("vec2-i1-w2",), autotune_copy=True)
    command = kernel.build_command(config, "/opt/bench")
    assert command[-3:] == ["--copy-variants", "vec2-i1-w2", "--autotune-copy"]


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
    assert "--copy-variants" not in command


def test_run_kernel_passes_timeout(monkeypatch: pytest.MonkeyPatch, fake_binary: str) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, SCHEMA_2_JSON, "")

    monkeypatch.setattr(kernel.subprocess, "run", run)
    kernel.run_kernel(_config(fake_binary, timeout_s=42.0))
    assert seen["timeout"] == 42.0


def test_summary_rows_cover_copy_and_gemm() -> None:
    rows = kernel.summary_rows(json.loads(SCHEMA_2_JSON))
    assert rows == [
        ["copy vec4-i1-w4", "block 128", "142.5 GB/s"],
        ["copy vec4-i1-w4", "block 256", "151.3 GB/s"],
        ["gemm fp32", "nn 1024", "4321.5 GFLOPS"],
        ["gemm fp16", "nt 2048", "12345.6 GFLOPS"],
    ]


def test_summary_rows_show_percent_after_roofline() -> None:
    rows = kernel.summary_rows(kernel.attach_roofline(json.loads(SCHEMA_2_JSON)))
    assert rows == [
        ["copy vec4-i1-w4", "block 128", "142.5 GB/s (22.8% peak)"],
        ["copy vec4-i1-w4", "block 256", "151.3 GB/s (24.2% peak)"],
        ["gemm fp32", "nn 1024", "4321.5 GFLOPS (11.6% peak)"],
        ["gemm fp16", "nt 2048", "12345.6 GFLOPS (16.5% peak)"],
    ]


def test_summary_rows_show_legacy_rows_without_variant() -> None:
    rows = kernel.summary_rows(json.loads(LEGACY_JSON))
    assert rows == [
        ["copy", "block 128", "142.5 GB/s"],
        ["copy", "block 256", "151.3 GB/s"],
        ["gemm fp32", "nn 1024", "4321.5 GFLOPS"],
        ["gemm fp16", "nt 2048", "12345.6 GFLOPS"],
    ]


def test_render_text_shows_device_and_tables() -> None:
    text = kernel.render_text(json.loads(SCHEMA_2_JSON))
    assert "AMD Radeon RX 7800 XT" in text
    assert "gfx1101" in text
    assert "6.2.41133" in text
    assert "6.2.0" in text
    assert "copy" in text
    assert "gemm" in text
    assert "vec4-i1-w4" in text
    assert "142.5" in text
    assert "12345.6" in text


def test_render_text_shows_percent_after_roofline() -> None:
    text = kernel.render_text(kernel.attach_roofline(json.loads(SCHEMA_2_JSON)))
    assert "22.8" in text
    assert "11.6" in text
    assert "16.5" in text
    assert "AMD Radeon RX 7800 XT" in text


def test_render_text_handles_legacy_rows() -> None:
    text = kernel.render_text(json.loads(LEGACY_JSON))
    assert "142.5" in text
    assert "12345.6" in text


def test_render_text_shows_clock_readings_when_present() -> None:
    data = json.loads(SCHEMA_2_JSON)
    data["clocks"] = {"gpu_mhz": 2400.0, "power_w": 250.0}
    text = kernel.render_text(data)
    assert "gpu_mhz=2400.0" in text
    assert "power_w=250.0" in text


def test_load_peaks_returns_gfx1101_table() -> None:
    peaks = kernel.load_peaks("gfx1101")
    assert peaks is not None
    assert peaks["name"] == "AMD Radeon RX 7800 XT"
    assert peaks["bandwidth_gbps"] == pytest.approx(624.0)
    assert peaks["fp32_tflops"] == pytest.approx(37.3)
    assert peaks["fp16_tflops"] == pytest.approx(74.6)


def test_load_peaks_returns_none_for_unknown_gfx() -> None:
    assert kernel.load_peaks("gfx9999") is None


def test_attach_roofline_sets_percentages() -> None:
    original = json.loads(SCHEMA_2_JSON)
    result = kernel.attach_roofline(original)
    assert "peaks" not in original
    assert result["peaks"]["bandwidth_gbps"] == pytest.approx(624.0)
    assert result["copy"][0]["pct_of_peak"] == pytest.approx(142.5 / 624.0 * 100.0)
    assert result["copy"][1]["pct_of_peak"] == pytest.approx(151.3 / 624.0 * 100.0)
    assert result["gemm"][0]["pct_of_peak"] == pytest.approx(4321.5 / (37.3 * 1000.0) * 100.0)
    assert result["gemm"][1]["pct_of_peak"] == pytest.approx(12345.6 / (74.6 * 1000.0) * 100.0)


def test_attach_roofline_keeps_every_other_row_field() -> None:
    original = json.loads(SCHEMA_2_JSON)
    result = kernel.attach_roofline(original)
    for source_row, result_row in zip(original["copy"], result["copy"], strict=True):
        for key, value in source_row.items():
            assert result_row[key] == value
    for source_row, result_row in zip(original["gemm"], result["gemm"], strict=True):
        for key, value in source_row.items():
            assert result_row[key] == value
    assert "pct_of_peak" not in original["copy"][0]


def test_attach_roofline_returns_input_unchanged_for_unknown_gfx() -> None:
    data = json.loads(SCHEMA_2_JSON)
    data["gfx"] = "gfx9999"
    result = kernel.attach_roofline(data)
    assert result is data
    assert "peaks" not in result
    assert "pct_of_peak" not in result["copy"][0]


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
    _fake_run(monkeypatch, stdout=_payload({field: []}))
    with pytest.raises(errors.UpstreamError):
        kernel.run_kernel(_config(fake_binary))


@pytest.mark.parametrize("overrides", INVALID_PAYLOADS)
def test_invalid_payload_field_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str, overrides: dict[str, Any]
) -> None:
    _fake_run(monkeypatch, stdout=_payload(overrides))
    with pytest.raises(errors.UpstreamError):
        kernel.run_kernel(_config(fake_binary))


def test_reported_errors_raise_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_binary: str
) -> None:
    _fake_run(monkeypatch, stdout=_payload({"errors": ["rocBLAS error: internal error"]}))
    with pytest.raises(errors.UpstreamError, match="internal error"):
        kernel.run_kernel(_config(fake_binary))


@pytest.mark.parametrize("overrides", INVALID_CONFIGS)
def test_invalid_config_raises_usage_error(fake_binary: str, overrides: dict[str, Any]) -> None:
    with pytest.raises(errors.UsageError):
        kernel.run_kernel(_config(fake_binary, **overrides))
