"""Locating and running the llama.cpp binaries.

The tool never installs anything. It finds the binaries on PATH or in the
directory named by `BLINDING_LLAMA_BIN`, records their versions, and fails with
a clear message when one is missing.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from blinding.errors import MissingDependency

SERVER_BINARY = "llama-server"
BENCH_BINARY = "llama-bench"
FINETUNE_BINARY = "llama-finetune"
PERPLEXITY_BINARY = "llama-perplexity"
TOOL_NAMES = (SERVER_BINARY, BENCH_BINARY, FINETUNE_BINARY, PERPLEXITY_BINARY)
TOOL_FIELDS = {
    SERVER_BINARY: "server",
    BENCH_BINARY: "bench",
    FINETUNE_BINARY: "finetune",
    PERPLEXITY_BINARY: "perplexity",
}
VERSION_TIMEOUT_S = 15.0


@dataclass(frozen=True, slots=True)
class LlamaTools:
    """Absolute paths to the llama.cpp binaries this host provides."""

    server: str | None
    bench: str | None
    finetune: str | None
    perplexity: str | None

    def require(self, name: str) -> str:
        """Return the path for `name` or raise with the expected name."""
        path = getattr(self, TOOL_FIELDS.get(name, name))
        if path is None:
            raise MissingDependency(
                f"{name} is not on PATH; point BLINDING_LLAMA_BIN at a llama.cpp build "
                "that includes the server and examples"
            )
        return path

    def as_dict(self) -> dict[str, str | None]:
        """Plain mapping for the run record."""
        return {
            "llama_server": self.server,
            "llama_bench": self.bench,
            "llama_finetune": self.finetune,
            "llama_perplexity": self.perplexity,
        }


def discover(binary_dir: Path | None = None) -> LlamaTools:
    """Find the llama.cpp binaries on PATH or in an explicit directory."""
    directory = binary_dir
    if directory is None:
        raw = os.environ.get("BLINDING_LLAMA_BIN", "")
        directory = Path(raw) if raw else None

    def locate(name: str) -> str | None:
        if directory is not None:
            candidate = directory / name
            return str(candidate) if candidate.is_file() else None
        return shutil.which(name)

    return LlamaTools(
        server=locate(SERVER_BINARY),
        bench=locate(BENCH_BINARY),
        finetune=locate(FINETUNE_BINARY),
        perplexity=locate(PERPLEXITY_BINARY),
    )


def version(path: str) -> str:
    """First version line a binary prints, or an empty string."""
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    output = (completed.stdout or completed.stderr).strip().splitlines()
    if not output:
        return ""
    first = output[0].strip()
    if first.lower().startswith(("usage:", "error:")):
        return ""
    return first


def versions(tools: LlamaTools) -> dict[str, str]:
    """Version strings for every binary that exists."""
    found: dict[str, str] = {}
    for name in TOOL_NAMES:
        path = getattr(tools, TOOL_FIELDS[name])
        if path is not None:
            found[name] = version(path)
    return found


def free_port() -> int:
    """An unused loopback port. The bind is a probe; llama.cpp takes the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def tail(path: Path, lines: int = 40) -> str:
    """Last `lines` lines of a log file, or an empty string."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])
