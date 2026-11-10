"""Host and device facts recorded beside every measurement.

A number without the machine that produced it is a claim, not a result. The
doctor collects what can be read without installing anything: CPU model, thread
count, memory, kernel, load average, llama.cpp versions, and the devices
llama.cpp reports.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blinding import proc

CPUINFO_PATH = Path("/proc/cpuinfo")
MEMINFO_PATH = Path("/proc/meminfo")
KFD_PATH = Path("/dev/kfd")
RENDER_GLOB = "/dev/dri/renderD*"
ROCMINFO = "rocminfo"
ROCM_SMI = "rocm-smi"
DEVICE_TIMEOUT_S = 30.0
MIB = 1024 * 1024
LAYER_PATTERN = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")
GFX_PATTERN = re.compile(r"\b(gfx[0-9a-f]+)\b")
MARKETING_PATTERN = re.compile(r"Marketing Name:\s*(.+)")


@dataclass(frozen=True, slots=True)
class HostInfo:
    """What the host is, independent of the workload."""

    hostname: str
    platform: str
    kernel: str
    cpu_model: str
    nproc: int
    memory_gib: float
    load1: float
    load5: float
    load15: float

    def as_dict(self) -> dict[str, object]:
        """Plain mapping for the run record."""
        return {
            "hostname": self.hostname,
            "platform": self.platform,
            "kernel": self.kernel,
            "cpu_model": self.cpu_model,
            "nproc": self.nproc,
            "memory_gib": self.memory_gib,
            "load1": self.load1,
            "load5": self.load5,
            "load15": self.load15,
        }


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One compute device as llama.cpp or ROCm names it."""

    backend: str
    name: str
    gfx: str | None
    memory_mib: int | None

    def as_dict(self) -> dict[str, object]:
        """Plain mapping for the run record."""
        return {
            "backend": self.backend,
            "name": self.name,
            "gfx": self.gfx,
            "memory_mib": self.memory_mib,
        }


def cpu_model() -> str:
    """Model name from /proc/cpuinfo, or the platform processor string."""
    try:
        for line in CPUINFO_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def memory_gib() -> float:
    """Total physical memory in GiB from /proc/meminfo."""
    try:
        for line in MEMINFO_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                kib = int(line.split()[1])
                return round(kib / (1024.0 * 1024.0), 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def host_info() -> HostInfo:
    """Collect the host fingerprint."""
    try:
        loads = os.getloadavg()
    except OSError:
        loads = (0.0, 0.0, 0.0)
    return HostInfo(
        hostname=platform.node() or "unknown",
        platform=platform.platform(),
        kernel=platform.release(),
        cpu_model=cpu_model(),
        nproc=os.cpu_count() or 1,
        memory_gib=memory_gib(),
        load1=loads[0],
        load5=loads[1],
        load15=loads[2],
    )


def _run_text(command: list[str], timeout: float = DEVICE_TIMEOUT_S) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "") + (completed.stderr or "")


def _gfx_from_rocminfo() -> str | None:
    if shutil.which(ROCMINFO) is None:
        return None
    text = _run_text([ROCMINFO])
    match = GFX_PATTERN.search(text)
    return match.group(1) if match else None


def discover_devices(tools: proc.LlamaTools) -> tuple[DeviceInfo, ...]:
    """Devices llama.cpp can see, with the gfx architecture when ROCm is present."""
    gfx = _gfx_from_rocminfo()
    if tools.server is None:
        return ()
    text = _run_text([tools.server, "--list-devices"])
    devices: list[DeviceInfo] = []
    pattern = re.compile(
        r"^\s*(?P<backend>[A-Za-z]+[0-9]*):\s*(?P<name>.+?)\s*"
        r"\((?P<free>\d+)\s*MiB,\s*(?P<total>\d+)\s*MiB",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        backend = match.group("backend")
        total = int(match.group("total"))
        devices.append(
            DeviceInfo(
                backend=backend,
                name=match.group("name").strip(),
                gfx=gfx if backend.lower().startswith(("rocm", "hip")) else None,
                memory_mib=total,
            )
        )
    return tuple(devices)


def rocm_version() -> str | None:
    """ROCm userspace version from the image, or None."""
    version_file = Path("/opt/rocm/.info/version")
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def device_nodes() -> dict[str, bool]:
    """Whether the GPU device nodes are visible in this container."""
    import glob

    return {
        "kfd": KFD_PATH.exists(),
        "render": bool(glob.glob(RENDER_GLOB)),
    }


def collect(tools: proc.LlamaTools, cache: Path) -> dict[str, Any]:
    """Full doctor report as a JSON-friendly mapping."""
    return {
        "host": host_info().as_dict(),
        "tools": proc.versions(tools),
        "tool_paths": tools.as_dict(),
        "devices": [device.as_dict() for device in discover_devices(tools)],
        "rocm_version": rocm_version(),
        "device_nodes": device_nodes(),
        "cache": str(cache),
    }


def format_text(report: dict[str, Any]) -> str:
    """Human-readable doctor output."""
    host = report["host"]
    lines = [
        f"host      {host['hostname']} ({host['platform']})",
        f"kernel    {host['kernel']}",
        f"cpu       {host['cpu_model']}",
        f"threads   {host['nproc']}",
        f"memory    {host['memory_gib']} GiB",
        f"cache     {report['cache']}",
    ]
    tools = report["tools"]
    for name, version in tools.items():
        lines.append(f"{name:<14} {version or 'unknown'}")
    rocm = report.get("rocm_version")
    if rocm:
        lines.append(f"rocm      {rocm}")
    nodes = report["device_nodes"]
    lines.append(f"device    kfd={nodes['kfd']} render={nodes['render']}")
    devices = report["devices"]
    if devices:
        for device in devices:
            gfx = device["gfx"] or "-"
            memory = device["memory_mib"]
            lines.append(
                f"gpu       {device['backend']} {device['name']} gfx={gfx} vram={memory} MiB"
            )
    else:
        lines.append("gpu       none reported by llama-server")
    return "\n".join(lines)
