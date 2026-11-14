"""Tests for the closed-loop benchmark against the fake endpoint.

The fake server can stretch its first-byte and inter-chunk delays, so these
tests check that the client's arrival timestamps reach the summary rather than
the server's own numbers. They also cover the two ways a prompt count is found
when the server has no /tokenize endpoint.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blinding import bench, proc
from blinding.config import ServerConfig
from blinding.server import LlamaServer


def _make_server(fake_server: Path, tmp_path: Path) -> LlamaServer:
    """Build a fake server context manager on a free port."""
    tools = proc.LlamaTools(server=str(fake_server), bench=None, finetune=None, perplexity=None)
    return LlamaServer(
        tools, ServerConfig(port=proc.free_port()), tmp_path / "tiny.gguf", "tiny", tmp_path
    )


def _config(base_url: str, *, requests: int = 1, max_tokens: int = 4) -> bench.BenchConfig:
    """A one-level run small enough to finish in a few hundred milliseconds."""
    return bench.BenchConfig(
        model="tiny",
        base_url=base_url,
        api_key=None,
        concurrency=(1,),
        requests_per_level=requests,
        warmup=0,
        prompt_tokens=8,
        max_tokens=max_tokens,
        min_successes=requests,
        max_error_rate=0.0,
    )


def test_bench_timing_calibration(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrival times follow the fake server's first-byte and chunk delays."""
    monkeypatch.setenv("FAKE_TTFT_S", "0.05")
    monkeypatch.setenv("FAKE_CHUNK_DELAY_S", "0.01")
    server = _make_server(fake_server, tmp_path)
    with server as running:
        result = asyncio.run(bench.run_bench(_config(running.base_url(), requests=2, max_tokens=8)))
    assert result.valid
    assert result.prompt_tokens_source == "tokenize"
    level = result.levels[0]
    assert level["ttft_ms"] is not None
    assert level["itl_ms"] is not None
    ttft_p50 = float(level["ttft_ms"]["p50"])
    itl_p50 = float(level["itl_ms"]["p50"])
    assert 25.0 <= ttft_p50 <= 150.0
    assert 4.0 <= itl_p50 <= 40.0


def test_bench_usage_probe_supplies_prompt_tokens(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without /tokenize the usage block supplies the prompt count."""
    monkeypatch.setenv("FAKE_NO_TOKENIZE", "1")
    monkeypatch.setenv("FAKE_PROMPT_TOKENS", "7")
    monkeypatch.setenv("FAKE_CHUNK_DELAY_S", "0.0")
    server = _make_server(fake_server, tmp_path)
    with server as running:
        result = asyncio.run(bench.run_bench(_config(running.base_url())))
    assert result.valid
    assert result.prompt_tokens == 7
    assert result.prompt_tokens_source == "usage_probe"


def test_bench_leaves_count_unmeasured_when_both_routes_fail(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tokenize endpoint and a failing chat leave the count unmeasured."""
    monkeypatch.setenv("FAKE_NO_TOKENIZE", "1")
    monkeypatch.setenv("FAKE_SERVER_FAIL", "1")
    server = _make_server(fake_server, tmp_path)
    with server as running:
        prompt, prompt_tokens, source = asyncio.run(bench.build_prompt(_config(running.base_url())))
    assert prompt
    assert prompt_tokens is None
    assert source == "unmeasured"


def test_bench_stream_options_fallback_keeps_samples(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream_options rejection costs one retry and leaves the samples ok."""
    monkeypatch.setenv("FAKE_REJECT_STREAM_OPTIONS", "1")
    monkeypatch.setenv("FAKE_CHUNK_DELAY_S", "0.0")
    server = _make_server(fake_server, tmp_path)
    with server as running:
        result = asyncio.run(bench.run_bench(_config(running.base_url(), requests=2)))
    assert result.valid
    assert [sample["ok"] for sample in result.samples] == [True, True]
