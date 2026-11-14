"""Tests for the CLI endpoint, compare, and gate paths."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from blinding import cli, proc
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
