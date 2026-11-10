"""Tests for run records, tables, and the results lookup."""

from __future__ import annotations

import json
from pathlib import Path

from blinding import proc, report


def _tools() -> proc.LlamaTools:
    return proc.LlamaTools(server=None, bench=None, finetune=None, perplexity=None)


def test_write_and_load_run(tmp_path: Path) -> None:
    """A run record round-trips with its report beside it."""
    run_dir = report.new_run_dir(tmp_path, "bench", "tiny q4")
    record = report.build_record(
        command="bench",
        argv=["blinding", "bench"],
        root=tmp_path,
        tools=_tools(),
        model={"id": "tiny", "quant": "q4"},
        config={"device": "cpu"},
        workload={"requests": 2},
        metrics={"levels": [{"concurrency": 1}]},
        validity={"valid": True, "reasons": []},
        artifacts={"report": "report.md"},
        started_at=report.utc_iso(),
        duration_s=1.0,
    )
    report.write_run(run_dir, record, "# Bench\n")
    loaded = report.load_run(run_dir / "run.json")
    assert loaded["schema_version"] == report.SCHEMA_VERSION
    assert loaded["validity"]["valid"] is True
    assert (run_dir / "report.md").read_text(encoding="utf-8").startswith("# Bench")


def test_table_escapes_pipes() -> None:
    """A pipe in a cell does not break the table."""
    table = report.markdown_table(["a"], [["x | y"]])
    assert "x \\| y" in table


def test_find_run_by_prefix(tmp_path: Path) -> None:
    """A selector can be a full directory name or a unique prefix."""
    run_dir = report.new_run_dir(tmp_path, "eval", "tiny")
    record = report.build_record(
        command="eval",
        argv=[],
        root=tmp_path,
        tools=_tools(),
        model=None,
        config=None,
        workload={},
        metrics={},
        validity={"valid": True, "reasons": []},
        artifacts={},
        started_at=report.utc_iso(),
        duration_s=0.0,
    )
    report.write_run(run_dir, record, "# Eval\n")
    found = report.find_run(tmp_path, run_dir.name[:16])
    assert Path(str(found["run_dir"])).name == run_dir.name
    assert json.loads((run_dir / "run.json").read_text())["command"] == "eval"
