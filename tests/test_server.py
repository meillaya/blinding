"""Tests for the llama-server argument builder and process supervisor."""

from __future__ import annotations

from pathlib import Path

import pytest

from blinding import proc
from blinding.config import ServerConfig
from blinding.server import LlamaServer, build_argv


def test_build_argv_carries_the_config() -> None:
    """Every config field that changes the kernel path appears in argv."""
    tools = proc.LlamaTools(server="/bin/true", bench=None, finetune=None, perplexity=None)
    config = ServerConfig(
        threads=6,
        threads_batch=3,
        ctx_size=2048,
        batch_size=256,
        ubatch_size=64,
        flash_attn="on",
        cache_type_k="q8_0",
        cache_type_v="q8_0",
        parallel=2,
        gpu_layers=99,
        port=9001,
    )
    argv = build_argv(tools, config, Path("/models/tiny.gguf"), "tiny", port=9100)
    joined = " ".join(argv)
    assert "--threads 6" in joined
    assert "--threads-batch 3" in joined
    assert "--ctx-size 2048" in joined
    assert "--batch-size 256" in joined
    assert "--ubatch-size 64" in joined
    assert "--flash-attn on" in joined
    assert "--cache-type-k q8_0" in joined
    assert "--n-gpu-layers 99" in joined
    assert "--port 9100" in joined
    assert "--metrics" in argv


def test_server_starts_and_stops(fake_server: Path, tmp_path: Path) -> None:
    """The supervisor waits for health, reads the log, and stops the process."""
    tools = proc.LlamaTools(server=str(fake_server), bench=None, finetune=None, perplexity=None)
    config = ServerConfig(threads=2, port=proc.free_port())
    with LlamaServer(tools, config, tmp_path / "tiny.gguf", "tiny", tmp_path) as running:
        assert running.base_url().startswith("http://127.0.0.1:")
        assert "listening" in running.log()
        assert running.offload() is None
    assert running.process.poll() is not None


def test_server_records_gpu_offload(fake_server: Path, tmp_path: Path) -> None:
    """A GPU config produces the offload line the run record reads."""
    tools = proc.LlamaTools(server=str(fake_server), bench=None, finetune=None, perplexity=None)
    config = ServerConfig(gpu_layers=99, port=proc.free_port())
    with LlamaServer(tools, config, tmp_path / "tiny.gguf", "tiny", tmp_path) as running:
        assert running.offload() == (99, 99)


def test_missing_server_binary_is_a_dependency_error(tmp_path: Path) -> None:
    """An empty tool set fails before a process starts."""
    tools = proc.LlamaTools(server=None, bench=None, finetune=None, perplexity=None)
    with pytest.raises(Exception, match="llama-server"):
        build_argv(tools, ServerConfig(), tmp_path / "tiny.gguf", "tiny")
