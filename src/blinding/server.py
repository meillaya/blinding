"""Starting, probing, and stopping llama-server.

The server runs in its own process group so Ctrl-C and the test harness can stop
it with its children. Readiness is the HTTP health endpoint, not a fixed sleep.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from contextlib import AbstractContextManager
from pathlib import Path

import httpx

from blinding import proc
from blinding.config import Device, ServerConfig
from blinding.errors import MissingDependency, UpstreamError

READY_POLL_S = 0.25
READY_TIMEOUT_S = 240.0
STOP_TIMEOUT_S = 15.0
HEALTH_TIMEOUT_S = 3.0
OFFLOAD_PATTERN = re.compile(r"offloaded\s+(\d+)/(\d+)\s+layers to GPU")


def build_argv(
    tools: proc.LlamaTools,
    config: ServerConfig,
    model_path: Path,
    alias: str,
    *,
    port: int | None = None,
) -> list[str]:
    """Translate a config into the llama-server argument list."""
    nproc = os.cpu_count() or 1
    argv = [
        tools.require("llama-server"),
        "--model",
        str(model_path),
        "--alias",
        alias,
        "--host",
        config.host,
        "--port",
        str(port if port is not None else config.port),
        "--ctx-size",
        str(config.ctx_size),
        "--batch-size",
        str(config.batch_size),
        "--ubatch-size",
        str(config.ubatch_size),
        "--cache-type-k",
        config.cache_type_k,
        "--cache-type-v",
        config.cache_type_v,
        "--flash-attn",
        str(config.flash_attn),
        "--parallel",
        str(config.parallel),
        "--metrics",
    ]
    threads = config.effective_threads(nproc)
    if config.threads > 0:
        argv.extend(["--threads", str(threads)])
    if config.threads_batch > 0:
        argv.extend(["--threads-batch", str(config.threads_batch)])
    if config.gpu_layers > 0:
        argv.extend(["--n-gpu-layers", str(config.gpu_layers)])
    argv.extend(config.extra_args)
    return argv


class RunningServer:
    """A started llama-server process and the paths that describe it."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        port: int,
        argv: list[str],
        config: ServerConfig,
        model_path: Path,
        log_path: Path,
    ) -> None:
        self.process = process
        self.port = port
        self.argv = argv
        self.config = config
        self.model_path = model_path
        self.log_path = log_path
        self._stopped = False

    def base_url(self) -> str:
        """Loopback base URL for the server."""
        return f"http://127.0.0.1:{self.port}"

    def chat_url(self) -> str:
        """OpenAI-compatible chat completion URL."""
        return f"{self.base_url()}/v1/chat/completions"

    def log(self) -> str:
        """Whole server log, or the last part if the file is missing."""
        return proc.tail(self.log_path, lines=200)

    def offload(self) -> tuple[int, int] | None:
        """GPU layers offloaded from the log, or None when the line is absent."""
        match = OFFLOAD_PATTERN.search(self.log())
        if match is None:
            return None
        return int(match.group(1)), int(match.group(2))

    def stop(self) -> None:
        """Stop the process group, escalating only if it ignores SIGTERM."""
        if self._stopped:
            return
        self._stopped = True
        if self.process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            self.process.terminate()
        try:
            self.process.wait(timeout=STOP_TIMEOUT_S)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            self.process.kill()
        try:
            self.process.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise MissingDependency("llama-server did not stop after SIGKILL") from None


class LlamaServer(AbstractContextManager[RunningServer]):
    """Context manager that starts a server, waits for health, and stops it."""

    def __init__(
        self,
        tools: proc.LlamaTools,
        config: ServerConfig,
        model_path: Path,
        alias: str,
        run_dir: Path,
        *,
        port: int | None = None,
        ready_timeout_s: float = READY_TIMEOUT_S,
    ) -> None:
        self.tools = tools
        self.config = config
        self.model_path = model_path
        self.alias = alias
        self.run_dir = run_dir
        self.port = port if port is not None else config.port
        self.ready_timeout_s = ready_timeout_s
        self.running: RunningServer | None = None

    def __enter__(self) -> RunningServer:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.run_dir / "server.log"
        port = self.port
        argv = build_argv(self.tools, self.config, self.model_path, self.alias, port=port)
        handle = log_path.open("wb")
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            handle.close()
        running = RunningServer(
            process,
            port=port,
            argv=argv,
            config=self.config,
            model_path=self.model_path,
            log_path=log_path,
        )
        self.running = running
        try:
            self._wait_ready(running)
        except BaseException:
            running.stop()
            raise
        return running

    def __exit__(self, *exc_info: object) -> bool:
        if self.running is not None:
            self.running.stop()
        return False

    def _wait_ready(self, running: RunningServer) -> None:
        deadline = time.monotonic() + self.ready_timeout_s
        url = f"{running.base_url()}/health"
        with httpx.Client(timeout=HEALTH_TIMEOUT_S) as client:
            while time.monotonic() < deadline:
                if running.process.poll() is not None:
                    raise MissingDependency(
                        "llama-server exited before becoming ready:\n" + running.log()
                    )
                try:
                    response = client.get(url)
                except httpx.HTTPError:
                    response = None
                if response is not None and response.status_code == httpx.codes.OK:
                    self._verify_models(running)
                    return
                time.sleep(READY_POLL_S)
        raise MissingDependency(
            f"llama-server was not ready within {self.ready_timeout_s:g}s:\n" + running.log()
        )

    def _verify_models(self, running: RunningServer) -> None:
        try:
            response = httpx.get(f"{running.base_url()}/v1/models", timeout=HEALTH_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"llama-server health passed but /v1/models failed: {exc}") from exc
        if response.status_code != httpx.codes.OK:
            raise UpstreamError(f"/v1/models returned HTTP {response.status_code}")


def looks_like_hip(config: ServerConfig) -> bool:
    """Whether the config asks for GPU offload."""
    return config.device is Device.HIP or config.gpu_layers > 0
