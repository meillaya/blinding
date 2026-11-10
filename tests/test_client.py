"""Tests for the streaming client and the benchmark's validity rule."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blinding import bench, client, proc
from blinding.config import ServerConfig
from blinding.server import LlamaServer


@pytest.fixture
def running_server(fake_server: Path, tmp_path: Path) -> LlamaServer:
    """A fake server context manager with a free port."""
    tools = proc.LlamaTools(server=str(fake_server), bench=None, finetune=None, perplexity=None)
    return LlamaServer(
        tools, ServerConfig(port=proc.free_port()), tmp_path / "tiny.gguf", "tiny", tmp_path
    )


def test_stream_chat_records_ttft_and_tokens(running_server: LlamaServer) -> None:
    """Streamed content produces a TTFT, inter-token gaps, and a token count."""
    with running_server as running:
        sample = asyncio.run(
            client.stream_chat(
                base_url=running.base_url(),
                model="tiny",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=8,
            )
        )
    assert sample.ok
    assert sample.status == 200
    assert sample.ttft_ms > 0
    assert sample.completion_tokens == 8
    assert sample.chunks >= 1
    assert len(sample.itl_ms) == sample.chunks - 1


def test_tokenize_and_models(running_server: LlamaServer) -> None:
    """The prompt count and the model list come from the server."""
    with running_server as running:
        tokens = asyncio.run(client.tokenize(base_url=running.base_url(), text="one two three"))
        models = asyncio.run(client.server_models(running.base_url()))
    assert tokens == 3
    assert models == ["tiny"]


def test_chat_once_returns_text(running_server: LlamaServer) -> None:
    """The eval path uses the non-streamed form."""
    with running_server as running:
        ok, status, text, error = asyncio.run(
            client.chat_once(
                base_url=running.base_url(),
                model="tiny",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=4,
            )
        )
    assert (ok, status, error) == (True, 200, "")
    assert text


def test_bench_valid_when_requests_succeed(running_server: LlamaServer) -> None:
    """A clean run over the fake server is valid."""
    with running_server as running:
        config = bench.BenchConfig(
            model="tiny",
            base_url=running.base_url(),
            api_key=None,
            concurrency=(1, 2),
            requests_per_level=2,
            warmup=0,
            prompt_tokens=8,
            max_tokens=4,
            min_successes=2,
            max_error_rate=0.0,
        )
        result = asyncio.run(bench.run_bench(config))
    assert result.valid
    assert not result.reasons
    assert len(result.levels) == 2


def test_bench_invalid_when_errors_exceed_threshold(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that answers 500 produces no summary row."""
    monkeypatch.setenv("FAKE_SERVER_FAIL", "1")
    tools = proc.LlamaTools(server=str(fake_server), bench=None, finetune=None, perplexity=None)
    server = LlamaServer(
        tools, ServerConfig(port=proc.free_port()), tmp_path / "tiny.gguf", "tiny", tmp_path
    )
    with server as running:
        config = bench.BenchConfig(
            model="tiny",
            base_url=running.base_url(),
            api_key=None,
            concurrency=(1,),
            requests_per_level=2,
            warmup=0,
            prompt_tokens=8,
            max_tokens=4,
            min_successes=2,
            max_error_rate=0.05,
        )
        result = asyncio.run(bench.run_bench(config))
    assert not result.valid
    assert any("error rate" in reason for reason in result.reasons)
