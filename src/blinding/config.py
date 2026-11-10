"""Server configuration, parsed from TOML and validated before use.

A config is the only place that names llama.cpp flags. `server.build_argv`
turns a config and a model path into an argument list, so the same config drives
the foreground server, the transient server a benchmark starts, and the tuning
probe.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from blinding.errors import ConfigError

DEFAULT_CONTEXT = 4096
DEFAULT_BATCH = 512
DEFAULT_UBATCH = 128
MIN_CONTEXT = 256
MAX_PORT = 65535
KV_CACHE_TYPES = frozenset({"f16", "f32", "bf16", "q8_0", "q4_0"})


class Device(StrEnum):
    """Compute backend selected for llama.cpp."""

    CPU = "cpu"
    HIP = "hip"


class FlashAttn(StrEnum):
    """llama.cpp flash attention setting."""

    AUTO = "auto"
    ON = "on"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """One llama.cpp server configuration."""

    name: str = "local-cpu"
    device: Device = Device.CPU
    threads: int = 0
    threads_batch: int = 0
    ctx_size: int = DEFAULT_CONTEXT
    batch_size: int = DEFAULT_BATCH
    ubatch_size: int = DEFAULT_UBATCH
    flash_attn: FlashAttn = FlashAttn.AUTO
    cache_type_k: str = "f16"
    cache_type_v: str = "f16"
    parallel: int = 1
    gpu_layers: int = 0
    host: str = "127.0.0.1"
    port: int = 8001
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigError("config name must not be empty")
        if self.threads < 0 or self.threads_batch < 0:
            raise ConfigError("thread counts must not be negative")
        if self.ctx_size < MIN_CONTEXT:
            raise ConfigError(f"ctx_size must be at least {MIN_CONTEXT}")
        if self.batch_size <= 0 or self.ubatch_size <= 0:
            raise ConfigError("batch_size and ubatch_size must be positive")
        if self.ubatch_size > self.batch_size:
            raise ConfigError("ubatch_size must not exceed batch_size")
        if self.parallel <= 0:
            raise ConfigError("parallel must be positive")
        if self.gpu_layers < 0:
            raise ConfigError("gpu_layers must not be negative")
        if not 1 <= self.port <= MAX_PORT:
            raise ConfigError(f"port must be between 1 and {MAX_PORT}")
        if not self.host:
            raise ConfigError("host must not be empty")
        if self.cache_type_k not in KV_CACHE_TYPES:
            raise ConfigError(f"unknown cache_type_k {self.cache_type_k!r}")
        if self.cache_type_v not in KV_CACHE_TYPES:
            raise ConfigError(f"unknown cache_type_v {self.cache_type_v!r}")
        for argument in self.extra_args:
            if not isinstance(argument, str) or not argument:
                raise ConfigError("extra_args must hold non-empty strings")

    def effective_threads(self, nproc: int) -> int:
        """Thread count to pass to llama.cpp; zero means every available core."""
        return self.threads if self.threads > 0 else nproc

    def with_device(self, device: Device) -> ServerConfig:
        """Return a copy for `device`, defaulting GPU layers to full offload."""
        gpu_layers = 99 if device is Device.HIP and self.gpu_layers == 0 else self.gpu_layers
        return replace(self, device=device, gpu_layers=gpu_layers)

    def as_dict(self) -> dict[str, object]:
        """Plain mapping for the run record."""
        return {
            "name": self.name,
            "device": str(self.device),
            "threads": self.threads,
            "threads_batch": self.threads_batch,
            "ctx_size": self.ctx_size,
            "batch_size": self.batch_size,
            "ubatch_size": self.ubatch_size,
            "flash_attn": str(self.flash_attn),
            "cache_type_k": self.cache_type_k,
            "cache_type_v": self.cache_type_v,
            "parallel": self.parallel,
            "gpu_layers": self.gpu_layers,
            "host": self.host,
            "port": self.port,
            "extra_args": list(self.extra_args),
        }


_CONFIG_FIELDS = frozenset(ServerConfig.__dataclass_fields__)


def _as_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value


def _as_str(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value


def load_config(path: Path) -> ServerConfig:
    """Parse and validate one TOML config at the boundary."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        parsed: Any = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"config {path} must be a TOML table")
    raw: dict[str, Any] = {str(key): value for key, value in parsed.items()}
    unknown = sorted(set(raw) - _CONFIG_FIELDS)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")

    device_raw = _as_str(raw, "device", Device.CPU.value)
    flash_raw = _as_str(raw, "flash_attn", FlashAttn.AUTO.value)
    try:
        device = Device(device_raw)
    except ValueError as exc:
        raise ConfigError(f"unknown device {device_raw!r}") from exc
    try:
        flash_attn = FlashAttn(flash_raw)
    except ValueError as exc:
        raise ConfigError(f"unknown flash_attn {flash_raw!r}") from exc

    extra_raw = raw.get("extra_args", [])
    if not isinstance(extra_raw, list) or not all(isinstance(item, str) for item in extra_raw):
        raise ConfigError("extra_args must be a list of strings")

    return ServerConfig(
        name=_as_str(raw, "name", "local-cpu"),
        device=device,
        threads=_as_int(raw, "threads", 0),
        threads_batch=_as_int(raw, "threads_batch", 0),
        ctx_size=_as_int(raw, "ctx_size", DEFAULT_CONTEXT),
        batch_size=_as_int(raw, "batch_size", DEFAULT_BATCH),
        ubatch_size=_as_int(raw, "ubatch_size", DEFAULT_UBATCH),
        flash_attn=flash_attn,
        cache_type_k=_as_str(raw, "cache_type_k", "f16"),
        cache_type_v=_as_str(raw, "cache_type_v", "f16"),
        parallel=_as_int(raw, "parallel", 1),
        gpu_layers=_as_int(raw, "gpu_layers", 0),
        host=_as_str(raw, "host", "127.0.0.1"),
        port=_as_int(raw, "port", 8001),
        extra_args=tuple(extra_raw),
    )


def default_config(device: Device = Device.CPU) -> ServerConfig:
    """The config used when no file is given."""
    if device is Device.HIP:
        return ServerConfig(name="local-rocm", device=device, gpu_layers=99)
    return ServerConfig()
