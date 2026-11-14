"""Tests for the streaming client and the benchmark's validity rule."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blinding import bench, client, proc
from blinding.config import ServerConfig
from blinding.server import LlamaServer


def _make_server(fake_server: Path, tmp_path: Path) -> LlamaServer:
    """Build a fake server context manager on a free port."""
    tools = proc.LlamaTools(server=str(fake_server), bench=None, finetune=None, perplexity=None)
    return LlamaServer(
        tools, ServerConfig(port=proc.free_port()), tmp_path / "tiny.gguf", "tiny", tmp_path
    )


@pytest.fixture
def running_server(fake_server: Path, tmp_path: Path) -> LlamaServer:
    """A fake server context manager with a free port."""
    return _make_server(fake_server, tmp_path)


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
    server = _make_server(fake_server, tmp_path)
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


def test_probe_prompt_tokens_reads_usage(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server without /tokenize still reports the count through usage."""
    monkeypatch.setenv("FAKE_NO_TOKENIZE", "1")
    monkeypatch.setenv("FAKE_PROMPT_TOKENS", "9")
    server = _make_server(fake_server, tmp_path)
    with server as running:
        probed = asyncio.run(
            client.probe_prompt_tokens(
                base_url=running.base_url(),
                model="tiny",
                text="one two three",
            )
        )
    assert probed == 9


def test_probe_prompt_tokens_returns_none_when_rejected(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected request leaves the prompt count unmeasured."""
    monkeypatch.setenv("FAKE_SERVER_FAIL", "1")
    server = _make_server(fake_server, tmp_path)
    with server as running:
        probed = asyncio.run(
            client.probe_prompt_tokens(
                base_url=running.base_url(),
                model="tiny",
                text="one two three",
            )
        )
    assert probed is None


def test_probe_prompt_tokens_returns_none_on_transport_error() -> None:
    """A closed port is a transport failure, not an exception."""
    probed = asyncio.run(
        client.probe_prompt_tokens(
            base_url=f"http://127.0.0.1:{proc.free_port()}",
            model="tiny",
            text="one two three",
        )
    )
    assert probed is None


def test_stream_chat_retries_without_stream_options(
    fake_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 400 for stream_options is retried once without that key."""
    monkeypatch.setenv("FAKE_REJECT_STREAM_OPTIONS", "1")
    monkeypatch.setenv("FAKE_CHUNK_DELAY_S", "0.0")
    server = _make_server(fake_server, tmp_path)
    with server as running:
        sample = asyncio.run(
            client.stream_chat(
                base_url=running.base_url(),
                model="tiny",
                messages=[{"role": "user", "content": "hello"}],
                max_tokens=4,
            )
        )
    assert sample.ok
    assert sample.status == 200
    assert sample.chunks == 4
