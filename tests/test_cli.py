"""Tests for the CLI endpoint, compare, and gate paths."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from blinding import cli, proc, profiler
from blinding.config import ServerConfig
from blinding.server import LlamaServer

RunFactory = Callable[..., Path]


@pytest.fixture
def running_server(fake_server: Path, tmp_path: Path) -> LlamaServer:
    """A fake llama-server on a free port."""
    tools = proc.LlamaTools(server=str(fake_server), bench=None, finetune=None, perplexity=None)
    return LlamaServer(
        tools, ServerConfig(port=proc.free_port()), tmp_path / "tiny.gguf", "tiny", tmp_path
    )


def _bench_argv(url: str, results: Path, manifest: Path, *extra: str) -> list[str]:
    return [
        "bench",
        "--url",
        url,
        "--model-name",
        "tiny",
        "--manifest",
        str(manifest),
        "--results",
        str(results),
        "--concurrency",
        "1",
        "--requests",
        "8",
        "--warmup",
        "0",
        "--prompt-tokens",
        "8",
        "--max-tokens",
        "4",
        *extra,
    ]


def test_bench_endpoint_mode_skips_the_manifest(
    running_server: LlamaServer, tmp_path: Path
) -> None:
    """An endpoint run needs no manifest entry and records endpoint provenance."""
    with running_server as running:
        exit_code = cli.main(
            _bench_argv(
                running.base_url(), tmp_path / "results", tmp_path / "missing.toml"
            )
        )
    assert exit_code == 0
    runs = [path for path in (tmp_path / "results").iterdir() if (path / "run.json").is_file()]
    assert len(runs) == 1
    record = json.loads((runs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["model"]["source"] == "endpoint"
    assert record["model"]["id"] == "tiny"
    assert record["config"] is None
    assert record["devices"] == []
    assert record["workload"]["prompt_tokens_source"] in {"tokenize", "usage_probe"}


def test_bench_endpoint_json_stdout(
    running_server: LlamaServer, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json prints the run record and keeps the table off stdout."""
    with running_server as running:
        exit_code = cli.main(
            _bench_argv(
                running.base_url(),
                tmp_path / "results",
                tmp_path / "missing.toml",
                "--json",
            )
        )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "bench"
    assert payload["schema_version"] == 2


def test_bench_unreachable_endpoint_is_upstream_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreachable URL without --model-name exits 5 and names the missing flag."""
    exit_code = cli.main(
        [
            "bench",
            "--url",
            "http://127.0.0.1:1",
            "--results",
            str(tmp_path / "results"),
            "--concurrency",
            "1",
            "--requests",
            "1",
            "--warmup",
            "0",
        ]
    )
    returned = capsys.readouterr().err
    assert exit_code == 5
    assert "--model-name" in returned


def test_compare_cli_prints_the_table(
    bench_run_factory: RunFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The compare command renders the table and returns zero."""
    root = tmp_path / "runs"
    baseline = bench_run_factory(root, "20261114T120000Z-bench-a", ttfts={1: [10.0] * 12})
    candidate = bench_run_factory(root, "20261114T130000Z-bench-b", ttfts={1: [10.2] * 12})
    exit_code = cli.main(
        ["compare", baseline.name, candidate.name, "--results", str(root)]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "ttft_p50_ms" in out
    assert "baseline" in out


def test_gate_cli_fails_on_a_regression(
    bench_run_factory: RunFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real latency regression exits 1 and JSON carries the gate block."""
    root = tmp_path / "runs"
    baseline = bench_run_factory(root, "20261114T120000Z-bench-a", ttfts={1: [10.0] * 12})
    candidate = bench_run_factory(root, "20261114T130000Z-bench-b", ttfts={1: [20.0] * 12})
    exit_code = cli.main(
        [
            "gate",
            "--baseline",
            baseline.name,
            "--candidate",
            candidate.name,
            "--results",
            str(root),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["gate"]["exit_code"] == 1
    assert payload["summary"]["verdict"] == "regressed"


def test_gate_cli_passes_identical_runs(
    bench_run_factory: RunFactory, tmp_path: Path
) -> None:
    """The default candidate selector works and a clean pair exits zero."""
    root = tmp_path / "runs"
    bench_run_factory(root, "20261114T120000Z-bench-a", ttfts={1: [10.0] * 12})
    newest = bench_run_factory(root, "20261114T130000Z-bench-b", ttfts={1: [10.0] * 12})
    exit_code = cli.main(
        ["gate", "--baseline", "20261114T120000Z-bench-a", "--results", str(root)]
    )
    assert exit_code == 0
    assert newest.exists()


def _kernel_payload() -> dict[str, object]:
    return {
        "bench_schema": 2,
        "device": "AMD Radeon RX 7800 XT",
        "gfx": "gfx1101",
        "rocm_version": "7.2.4",
        "errors": [],
        "copy": [
            {
                "variant": "vec4-i1-w4",
                "block": 128,
                "gbps": 400.0,
                "samples": [400.0, 401.0, 399.0, 400.5, 399.5, 400.0, 400.2],
            }
        ],
        "gemm": [
            {
                "dtype": "fp16",
                "layout": "nn",
                "size": 1024,
                "gflops": 30000.0,
                "samples": [30000.0, 30100.0, 29900.0, 30050.0, 29950.0, 30000.0, 30020.0],
            }
        ],
    }


def _profile_summary() -> profiler.ProfileSummary:
    return profiler.ProfileSummary(
        total_s=1.5,
        kernels=[
            profiler.KernelStat(
                name="rocblas_hgemm",
                calls=3,
                total_s=1.0,
                mean_s=1.0 / 3.0,
                p50_s=0.3,
                share=2.0 / 3.0,
            )
        ],
        trace_files=["trace/kernel_trace.csv"],
    )


def test_kernel_command_records_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel command can attach a profiled kernel breakdown to its record."""
    from blinding import kernel

    monkeypatch.setattr(kernel, "run_kernel", lambda config: _kernel_payload())
    monkeypatch.setattr(kernel, "resolve_binary", lambda config: "/tmp/bench")
    monkeypatch.setattr(
        profiler,
        "run_profiled",
        lambda argv, output_dir, timeout_s=600.0: _profile_summary(),
    )
    exit_code = cli.main(
        [
            "kernel",
            "--profile",
            "--binary",
            "/tmp/bench",
            "--blocks",
            "128",
            "--sizes",
            "1024",
            "--dtypes",
            "fp16",
            "--layouts",
            "nn",
            "--results",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 0
    runs = [path for path in (tmp_path / "results").iterdir() if (path / "run.json").is_file()]
    record = json.loads((runs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["metrics"]["profile"]["kernels"][0]["name"] == "rocblas_hgemm"
    assert record["metrics"]["copy"][0]["pct_of_peak"] is not None


def test_profile_command_records_kernel_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile command resolves a pinned model and records the kernel table."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    benchmark = bin_dir / "llama-bench"
    benchmark.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    benchmark.chmod(0o755)
    monkeypatch.setattr(cli, "_ensure", lambda model, quant, cache: (tmp_path / "tiny.gguf",))
    monkeypatch.setattr(
        profiler,
        "run_profiled",
        lambda argv, output_dir, timeout_s=600.0: _profile_summary(),
    )
    exit_code = cli.main(
        [
            "profile",
            "qwen2.5-0.5b-instruct:q4_k_m",
            "--llama-bin",
            str(bin_dir),
            "--config",
            str(cli.ROOT / "configs" / "local-rocm.toml"),
            "--results",
            str(tmp_path / "results"),
        ]
    )
    assert exit_code == 0
    runs = [path for path in (tmp_path / "results").iterdir() if (path / "run.json").is_file()]
    record = json.loads((runs[0] / "run.json").read_text(encoding="utf-8"))
    assert record["command"] == "profile"
    assert record["metrics"]["kernels"][0]["name"] == "rocblas_hgemm"
