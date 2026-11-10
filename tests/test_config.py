"""Tests for the config boundary: valid files load, bad values fail loudly."""

from __future__ import annotations

from pathlib import Path

import pytest

from blinding.config import Device, FlashAttn, ServerConfig, default_config, load_config
from blinding.errors import ConfigError


def test_load_toml_reads_every_field(tmp_path: Path) -> None:
    """A complete config maps to every field, including extra_args."""
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                'name = "custom"',
                'device = "hip"',
                "threads = 6",
                "threads_batch = 4",
                "ctx_size = 2048",
                "batch_size = 256",
                "ubatch_size = 64",
                'flash_attn = "on"',
                'cache_type_k = "q8_0"',
                'cache_type_v = "q8_0"',
                "parallel = 2",
                "gpu_layers = 99",
                'host = "0.0.0.0"',
                "port = 9000",
                'extra_args = ["--no-webui"]',
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.device is Device.HIP
    assert config.flash_attn is FlashAttn.ON
    assert config.ubatch_size == 64
    assert config.extra_args == ("--no-webui",)


def test_load_toml_rejects_unknown_keys(tmp_path: Path) -> None:
    """A typo in a config fails before the server starts."""
    path = tmp_path / "config.toml"
    path.write_text("thread = 4\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(path)


def test_ubatch_must_not_exceed_batch() -> None:
    """The constraint llama.cpp enforces is checked here too."""
    with pytest.raises(ConfigError, match="ubatch_size"):
        ServerConfig(batch_size=64, ubatch_size=128)


def test_kv_cache_type_is_checked() -> None:
    """An unknown cache type is a config error, not a late server crash."""
    with pytest.raises(ConfigError, match="cache_type_k"):
        ServerConfig(cache_type_k="float128")


def test_with_device_defaults_to_full_offload() -> None:
    """Selecting HIP without a layer count means every layer goes to the GPU."""
    config = default_config().with_device(Device.HIP)
    assert config.gpu_layers == 99
    assert config.device is Device.HIP
