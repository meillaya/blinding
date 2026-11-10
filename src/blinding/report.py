"""Run records, provenance, and generated reports.

The run record is the unit of evidence. Every command writes one before it
prints a summary, and the README table is copied from the records that passed
their validity check.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from blinding import __version__, doctor, proc

SCHEMA_VERSION = 1
GIT_TIMEOUT_S = 15.0
SLUG_PATTERN = re.compile(r"[^a-z0-9._-]+")
MAX_TABLE_CELL = 80


def repo_root() -> Path:
    """Repository root, overridable for tests with BLINDING_ROOT."""
    import os

    raw = os.environ.get("BLINDING_ROOT", "")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def git_info(root: Path) -> dict[str, object]:
    """Commit hash and whether tracked files differ from it."""
    commit = ""
    dirty = False
    try:
        commit_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        commit = commit_result.stdout.strip()
        status_result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        dirty = bool(status_result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        dirty = True
    return {"commit": commit, "dirty": dirty}


def tool_info(tools: proc.LlamaTools) -> dict[str, object]:
    """Binary paths and version strings."""
    return {"paths": tools.as_dict(), "versions": proc.versions(tools)}


def utc_stamp() -> str:
    """Compact UTC timestamp used in run directory names."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    """RFC 3339 UTC timestamp for the record."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_slug(raw: str) -> str:
    """Lowercase path-safe slug for a run directory."""
    slug = SLUG_PATTERN.sub("-", raw.strip().lower()).strip("-._")
    return slug[:60] or "run"


def new_run_dir(results_root: Path, command: str, slug: str) -> Path:
    """Create a unique run directory under `results_root`."""
    base = results_root / f"{utc_stamp()}-{safe_slug(command)}-{safe_slug(slug)}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def build_record(
    *,
    command: str,
    argv: list[str],
    root: Path,
    tools: proc.LlamaTools,
    model: dict[str, object] | None,
    config: dict[str, object] | None,
    workload: dict[str, object],
    metrics: dict[str, object],
    validity: dict[str, object],
    artifacts: dict[str, str],
    started_at: str,
    duration_s: float,
) -> dict[str, object]:
    """Assemble the common run record shape."""
    return {
        "schema_version": SCHEMA_VERSION,
        "blinding_version": __version__,
        "command": command,
        "argv": argv,
        "started_at": started_at,
        "duration_s": round(duration_s, 3),
        "git": git_info(root),
        "host": doctor.host_info().as_dict(),
        "tools": tool_info(tools),
        "devices": [device.as_dict() for device in doctor.discover_devices(tools)],
        "model": model,
        "config": config,
        "workload": workload,
        "metrics": metrics,
        "validity": validity,
        "artifacts": artifacts,
    }


def write_run(run_dir: Path, record: dict[str, object], report: str) -> Path:
    """Write run.json and report.md into an existing run directory."""
    run_path = run_dir / "run.json"
    report_path = run_dir / "report.md"
    run_path.write_text(
        json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(report.rstrip() + "\n", encoding="utf-8")
    return run_path


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a pipe table with escaped cells."""

    def cell(value: str) -> str:
        text = value.replace("|", "\\|").replace("\n", " ")
        return text[:MAX_TABLE_CELL]

    lines = ["| " + " | ".join(cell(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    return "\n".join(lines)


def load_run(path: Path) -> dict[str, Any]:
    """Load one run.json."""
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} does not hold a run object")
    return {str(key): value for key, value in raw.items()}


def run_dirs(results_root: Path) -> list[Path]:
    """Every directory under `results_root` holding a run.json, newest first."""
    if not results_root.is_dir():
        return []
    found = [path for path in results_root.iterdir() if (path / "run.json").is_file()]
    return sorted(found, reverse=True)


def list_runs(results_root: Path) -> list[dict[str, Any]]:
    """Load every run record, newest first."""
    return [load_run(path / "run.json") | {"run_dir": str(path)} for path in run_dirs(results_root)]


def find_run(results_root: Path, selector: str) -> dict[str, Any]:
    """Find a run by directory name or unique prefix."""
    runs = list_runs(results_root)
    exact = [run for run in runs if Path(str(run["run_dir"])).name == selector]
    if exact:
        return exact[0]
    partial = [run for run in runs if Path(str(run["run_dir"])).name.startswith(selector)]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ValueError(f"no run matches {selector!r}")
    names = ", ".join(Path(str(run["run_dir"])).name for run in partial)
    raise ValueError(f"ambiguous run {selector!r}; matches: {names}")


def metric_value(run: dict[str, Any], key: str) -> Any:
    """Dig a dotted metric path out of a run record."""
    current: Any = run
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current
