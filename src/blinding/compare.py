"""Compare two run records and report whether the change is real.

The compare command reads the run records and the per-request samples the
benchmark writes. A metric difference carries a percentile bootstrap interval
over the request samples, and the verdict follows from the interval: a change
counts as a regression only when the whole interval sits beyond the tolerance.

Kernel runs are compared from their per-iteration samples once the kernel
benchmark records them. Profile runs are inspection material and do not gate.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blinding import report, stats
from blinding.errors import InvalidMeasurement

SCHEMA = "blinding.compare/1"
BENCH_COMMAND = "bench"
KERNEL_COMMAND = "kernel"
PROFILE_COMMAND = "profile"

LOWER_IS_BETTER = "lower"
HIGHER_IS_BETTER = "higher"


@dataclass(frozen=True, slots=True)
class CompareOptions:
    """Thresholds and statistics settings for one comparison."""

    resamples: int = stats.DEFAULT_RESAMPLES
    confidence: float = stats.DEFAULT_CONFIDENCE
    seed: int = stats.DEFAULT_SEED
    max_regression: float = 5.0
    min_samples: int = 8
    allow_host_change: bool = False
    allow_device_change: bool = False
    require_clean: bool = False
    fail_on_point: bool = False
    fail_on_inconclusive: bool = False


@dataclass(slots=True)
class RunData:
    """One run directory, its record, and the optional request samples."""

    path: Path
    record: dict[str, Any]
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.path.name

    @property
    def command(self) -> str:
        return str(self.record.get("command", ""))

    @property
    def valid(self) -> bool:
        validity = self.record.get("validity")
        return isinstance(validity, dict) and bool(validity.get("valid", False))

    def levels(self) -> list[dict[str, Any]]:
        return _record_levels(self.record, self.run_id)

    def level(self, concurrency: int) -> dict[str, Any] | None:
        for level in self.levels():
            if level.get("concurrency") == concurrency:
                return level
        return None


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One metric compared across the two runs."""

    name: str
    unit: str
    direction: str
    baseline: float | None
    candidate: float | None
    delta_pct: float | None
    low_pct: float | None
    high_pct: float | None
    n_baseline: int
    n_candidate: int
    source: str
    verdict: str
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "direction": self.direction,
            "baseline": _round(self.baseline),
            "candidate": _round(self.candidate),
            "delta_pct": _round(self.delta_pct),
            "ci_low_pct": _round(self.low_pct),
            "ci_high_pct": _round(self.high_pct),
            "n_baseline": self.n_baseline,
            "n_candidate": self.n_candidate,
            "source": self.source,
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RowResult:
    """One kernel row compared across the two runs."""

    benchmark: str
    detail: str
    unit: str
    baseline: float | None
    candidate: float | None
    delta_pct: float | None
    low_pct: float | None
    high_pct: float | None
    n_baseline: int
    n_candidate: int
    source: str
    verdict: str
    reason: str = ""

    @property
    def label(self) -> str:
        return f"{self.benchmark} {self.detail}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "detail": self.detail,
            "unit": self.unit,
            "baseline": _round(self.baseline),
            "candidate": _round(self.candidate),
            "delta_pct": _round(self.delta_pct),
            "ci_low_pct": _round(self.low_pct),
            "ci_high_pct": _round(self.high_pct),
            "n_baseline": self.n_baseline,
            "n_candidate": self.n_candidate,
            "source": self.source,
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class LevelResult:
    """All metric results for one shared concurrency level."""

    concurrency: int
    metrics: tuple[MetricResult, ...]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """The provenance a reader needs beside the numbers."""

    run_id: str
    command: str
    started_at: str
    git_commit: str
    git_dirty: bool
    valid: bool
    validity_reasons: tuple[str, ...]
    host: dict[str, Any]
    devices: list[Any]
    model: dict[str, Any] | None
    config: dict[str, Any] | None
    tools: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command": self.command,
            "started_at": self.started_at,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "valid": self.valid,
            "validity_reasons": list(self.validity_reasons),
            "host": self.host,
            "devices": self.devices,
            "model": self.model,
            "config": self.config,
            "tools": self.tools,
        }


@dataclass(frozen=True, slots=True)
class Compatibility:
    """Why the two runs can or cannot be compared."""

    failures: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "failures": list(self.failures), "warnings": list(self.warnings)}


@dataclass(frozen=True, slots=True)
class CompareReport:
    """Everything the CLI needs to print or gate one comparison."""

    baseline: RunSummary
    candidate: RunSummary
    compatibility: Compatibility
    context: dict[str, Any]
    levels: tuple[LevelResult, ...]
    skipped_levels: tuple[int, ...]
    options: CompareOptions
    command: str
    rows: tuple[RowResult, ...] = ()
    skipped_rows: tuple[str, ...] = ()

    def metrics(self) -> list[MetricResult]:
        return [metric for level in self.levels for metric in level.metrics]

    def verdicts(self) -> list[str]:
        return [metric.verdict for metric in self.metrics()] + [
            row.verdict for row in self.rows
        ]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for verdict in self.verdicts():
            counts[verdict] = counts.get(verdict, 0) + 1
        return counts

    def overall_verdict(self) -> str:
        if not self.compatibility.ok:
            return "invalid"
        verdicts = self.verdicts()
        if "regressed" in verdicts:
            return "regressed"
        if "point_regressed" in verdicts:
            return "point_regressed"
        if any(verdict in {"invalid", "missing"} for verdict in verdicts):
            return "invalid"
        if "inconclusive" in verdicts:
            return "inconclusive"
        if any(verdict in {"improved", "point_improved"} for verdict in verdicts):
            return "improved"
        return "unchanged"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "command": self.command,
            "options": {
                "resamples": self.options.resamples,
                "confidence": self.options.confidence,
                "seed": self.options.seed,
                "max_regression": self.options.max_regression,
                "min_samples": self.options.min_samples,
                "allow_host_change": self.options.allow_host_change,
                "allow_device_change": self.options.allow_device_change,
                "require_clean": self.options.require_clean,
                "fail_on_point": self.options.fail_on_point,
                "fail_on_inconclusive": self.options.fail_on_inconclusive,
            },
            "baseline": self.baseline.as_dict(),
            "candidate": self.candidate.as_dict(),
            "compatibility": self.compatibility.as_dict(),
            "context": self.context,
            "levels": [
                {
                    "concurrency": level.concurrency,
                    "metrics": [metric.as_dict() for metric in level.metrics],
                }
                for level in self.levels
            ],
            "skipped_levels": list(self.skipped_levels),
            "rows": [row.as_dict() for row in self.rows],
            "skipped_rows": list(self.skipped_rows),
            "summary": {
                "verdict": self.overall_verdict(),
                "counts": self.counts(),
            },
            "gate": {
                "exit_code": gate_exit_code(self),
                "failures": gate_failures(self),
            },
        }


def load_run_data(path: Path) -> RunData:
    """Load one run directory: record first, request samples when present."""
    run_path = path if path.is_dir() else path.parent
    record_file = run_path / "run.json"
    if not record_file.is_file():
        raise InvalidMeasurement(f"{run_path} does not hold a run.json")
    try:
        record = report.load_run(record_file)
    except (OSError, ValueError) as exc:
        raise InvalidMeasurement(f"cannot read {record_file}: {exc}") from exc
    if not record.get("command"):
        raise InvalidMeasurement(f"{record_file} does not name a command")
    return RunData(path=run_path, record=record, requests=load_requests(run_path / "requests.csv"))


def load_requests(path: Path) -> list[dict[str, Any]]:
    """Parse requests.csv, skipping rows the benchmark did not complete cleanly."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                parsed = _parse_request_row(raw)
                if parsed is not None:
                    rows.append(parsed)
    except (OSError, csv.Error):
        return []
    return rows


def _parse_request_row(raw: dict[str, str]) -> dict[str, Any] | None:
    try:
        concurrency = int(raw.get("concurrency", "0"))
    except ValueError:
        return None
    if concurrency <= 0:
        return None
    ok = str(raw.get("ok", "")).strip().lower() in {"true", "1", "yes"}
    ttft = _as_float(raw.get("ttft_ms"))
    latency = _as_float(raw.get("latency_ms"))
    completion = _as_int(raw.get("completion_tokens"))
    itl_raw = raw.get("itl_ms", "")
    try:
        decoded: Any = json.loads(itl_raw) if itl_raw else []
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list):
        return None
    itl = [value for value in (_as_float(item) for item in decoded) if value is not None]
    return {
        "concurrency": concurrency,
        "ok": ok,
        "ttft_ms": ttft,
        "latency_ms": latency,
        "completion_tokens": completion,
        "itl_ms": itl,
        "error": raw.get("error", ""),
    }


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed != float("inf") else None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _record_levels(record: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        raise InvalidMeasurement(f"{run_id}: the record has no metrics object")
    levels = metrics.get("levels")
    if not isinstance(levels, list):
        raise InvalidMeasurement(f"{run_id}: the record has no metrics.levels list")
    result: list[dict[str, Any]] = []
    for level in levels:
        if not isinstance(level, dict):
            raise InvalidMeasurement(f"{run_id}: a metrics.levels entry is not an object")
        concurrency = level.get("concurrency")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            raise InvalidMeasurement(f"{run_id}: a metrics.levels entry has no integer concurrency")
        result.append(level)
    return result


def _summary(run: RunData) -> RunSummary:
    record = run.record
    raw_git = record.get("git")
    git: dict[str, Any] = raw_git if isinstance(raw_git, dict) else {}
    raw_host = record.get("host")
    host: dict[str, Any] = raw_host if isinstance(raw_host, dict) else {}
    raw_validity = record.get("validity")
    validity: dict[str, Any] = raw_validity if isinstance(raw_validity, dict) else {}
    raw_reasons = validity.get("reasons")
    reasons: list[Any] = raw_reasons if isinstance(raw_reasons, list) else []
    devices = record.get("devices")
    model = record.get("model")
    config = record.get("config")
    tools = record.get("tools")
    return RunSummary(
        run_id=run.run_id,
        command=run.command,
        started_at=str(record.get("started_at", "")),
        git_commit=str(git.get("commit", "")),
        git_dirty=bool(git.get("dirty", False)),
        valid=run.valid,
        validity_reasons=tuple(str(reason) for reason in reasons),
        host={str(key): value for key, value in host.items()},
        devices=devices if isinstance(devices, list) else [],
        model={str(key): value for key, value in model.items()}
        if isinstance(model, dict)
        else None,
        config={str(key): value for key, value in config.items()}
        if isinstance(config, dict)
        else None,
        tools={str(key): value for key, value in tools.items()} if isinstance(tools, dict) else {},
    )


def _device_signatures(devices: list[Any]) -> list[str]:
    signatures: list[str] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        name = str(device.get("name", ""))
        gfx = str(device.get("gfx", ""))
        backend = str(device.get("backend", ""))
        signatures.append(f"{backend}:{name}:{gfx}")
    return sorted(signatures)


def _compatibility(
    baseline: RunData, candidate: RunData, options: CompareOptions
) -> Compatibility:
    failures: list[str] = []
    warnings: list[str] = []
    base = _summary(baseline)
    cand = _summary(candidate)
    if baseline.command != candidate.command:
        failures.append(f"commands differ: {baseline.command} against {candidate.command}")
    if not base.valid:
        failures.append(
            "baseline run is marked invalid: " + ", ".join(base.validity_reasons or ("no reason",))
        )
    if not cand.valid:
        failures.append(
            "candidate run is marked invalid: "
            + ", ".join(cand.validity_reasons or ("no reason",))
        )
    if not options.allow_host_change:
        fields = ("hostname", "cpu_model")
        changes = [
            f"host {field}: {base.host.get(field)} against {cand.host.get(field)}"
            for field in fields
            if base.host.get(field) != cand.host.get(field)
        ]
        if changes:
            failures.extend(changes)
    if not options.allow_device_change:
        base_devices = _device_signatures(base.devices)
        cand_devices = _device_signatures(cand.devices)
        if base_devices and cand_devices and base_devices != cand_devices:
            failures.append(
                "devices differ: "
                f"{', '.join(base_devices)} against {', '.join(cand_devices)}"
            )
    if options.require_clean:
        if base.git_dirty:
            failures.append("baseline tree is dirty and --require-clean was given")
        if cand.git_dirty:
            failures.append("candidate tree is dirty and --require-clean was given")
    if base.git_dirty or cand.git_dirty:
        warnings.append("a run recorded a dirty tree")
    if baseline.record.get("blinding_version") != candidate.record.get("blinding_version"):
        warnings.append("the runs were written by different Blinding versions")
    return Compatibility(tuple(failures), tuple(warnings))


def _context(baseline: RunData, candidate: RunData) -> dict[str, Any]:
    base = _summary(baseline)
    cand = _summary(candidate)
    config_changes = _dict_changes(base.config, cand.config)
    tool_changes = _dict_changes(base.tools, cand.tools)
    model_changes = _dict_changes(base.model, cand.model)
    git_changes = []
    if base.git_commit != cand.git_commit:
        before = base.git_commit[:12] or "unknown"
        after = cand.git_commit[:12] or "unknown"
        git_changes.append(f"{before} -> {after}")
    loads = {
        side: {
            key: host.get(key)
            for key in ("load1", "load5", "load15")
            if isinstance(host, dict) and key in host
        }
        for side, host in (("baseline", base.host), ("candidate", cand.host))
    }
    return {
        "config_changes": config_changes,
        "git_changes": git_changes,
        "tool_changes": tool_changes,
        "model_changes": model_changes,
        "load": loads,
    }


def _dict_changes(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> list[str]:
    if before is None and after is None:
        return []
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    changes: list[str] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changes.append(f"{key}: {old} -> {new}")
    return changes


def _samples(
    run: RunData, concurrency: int
) -> tuple[list[float], list[list[float]], int]:
    rows = [
        row
        for row in run.requests
        if row["concurrency"] == concurrency and row["ok"]
    ]
    ttfts = [
        row["ttft_ms"]
        for row in rows
        if isinstance(row["ttft_ms"], float) and row["ttft_ms"] > 0.0
    ]
    itl_groups = [row["itl_ms"] for row in rows if row["itl_ms"]]
    return (ttfts, itl_groups, len(rows))


def _relative(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None:
        return None
    if baseline == 0.0:
        return 0.0 if candidate == 0.0 else None
    return (candidate - baseline) / baseline * 100.0


def _interval_verdict(
    point: float | None,
    low: float | None,
    high: float | None,
    direction: str,
    tolerance: float,
) -> str:
    if point is None or low is None or high is None:
        return "inconclusive"
    if direction == LOWER_IS_BETTER:
        if low > tolerance:
            return "regressed"
        if high < -tolerance:
            return "improved"
    else:
        if high < -tolerance:
            return "regressed"
        if low > tolerance:
            return "improved"
    if abs(point) > tolerance:
        return "inconclusive"
    return "unchanged"


def _point_verdict(point: float | None, direction: str, tolerance: float) -> str:
    if point is None:
        return "inconclusive"
    worse = point > tolerance if direction == LOWER_IS_BETTER else point < -tolerance
    better = point < -tolerance if direction == LOWER_IS_BETTER else point > tolerance
    if worse:
        return "point_regressed"
    if better:
        return "point_improved"
    return "point_unchanged"


def _interval_metric(
    name: str,
    unit: str,
    direction: str,
    percentile: float,
    baseline_values: list[float],
    candidate_values: list[float],
    options: CompareOptions,
) -> MetricResult:
    point = _relative(
        stats.percentile(baseline_values, percentile) if baseline_values else None,
        stats.percentile(candidate_values, percentile) if candidate_values else None,
    )
    if (
        len(baseline_values) < options.min_samples
        or len(candidate_values) < options.min_samples
    ):
        return MetricResult(
            name=name,
            unit=unit,
            direction=direction,
            baseline=stats.percentile(baseline_values, percentile) if baseline_values else None,
            candidate=stats.percentile(candidate_values, percentile) if candidate_values else None,
            delta_pct=point,
            low_pct=None,
            high_pct=None,
            n_baseline=len(baseline_values),
            n_candidate=len(candidate_values),
            source="requests",
            verdict="inconclusive",
            reason=f"fewer than {options.min_samples} samples on a side",
        )
    delta = stats.bootstrap_delta_ci(
        baseline_values,
        candidate_values,
        percentile,
        resamples=options.resamples,
        confidence=options.confidence,
        seed=options.seed,
    )
    return MetricResult(
        name=name,
        unit=unit,
        direction=direction,
        baseline=stats.percentile(baseline_values, percentile),
        candidate=stats.percentile(candidate_values, percentile),
        delta_pct=delta.point_pct,
        low_pct=delta.low_pct,
        high_pct=delta.high_pct,
        n_baseline=len(baseline_values),
        n_candidate=len(candidate_values),
        source="requests",
        verdict=_interval_verdict(
            delta.point_pct, delta.low_pct, delta.high_pct, direction, options.max_regression
        ),
    )


def _cluster_metric(
    name: str,
    unit: str,
    percentile: float,
    baseline_groups: list[list[float]],
    candidate_groups: list[list[float]],
    options: CompareOptions,
) -> MetricResult:
    baseline_count = len(baseline_groups)
    candidate_count = len(candidate_groups)
    baseline_value = stats.percentile(
        [value for group in baseline_groups for value in group], percentile
    ) if baseline_groups else None
    candidate_value = stats.percentile(
        [value for group in candidate_groups for value in group], percentile
    ) if candidate_groups else None
    point = _relative(baseline_value, candidate_value)
    if baseline_count < options.min_samples or candidate_count < options.min_samples:
        return MetricResult(
            name=name,
            unit=unit,
            direction=LOWER_IS_BETTER,
            baseline=baseline_value,
            candidate=candidate_value,
            delta_pct=point,
            low_pct=None,
            high_pct=None,
            n_baseline=baseline_count,
            n_candidate=candidate_count,
            source="requests",
            verdict="inconclusive",
            reason=f"fewer than {options.min_samples} request groups on a side",
        )
    delta = stats.cluster_bootstrap_delta_ci(
        baseline_groups,
        candidate_groups,
        percentile,
        resamples=options.resamples,
        confidence=options.confidence,
        seed=options.seed,
    )
    return MetricResult(
        name=name,
        unit=unit,
        direction=LOWER_IS_BETTER,
        baseline=baseline_value,
        candidate=candidate_value,
        delta_pct=delta.point_pct,
        low_pct=delta.low_pct,
        high_pct=delta.high_pct,
        n_baseline=baseline_count,
        n_candidate=candidate_count,
        source="requests",
        verdict=_interval_verdict(
            delta.point_pct, delta.low_pct, delta.high_pct, LOWER_IS_BETTER, options.max_regression
        ),
    )


def _record_metric(
    name: str,
    unit: str,
    direction: str,
    baseline_value: float | None,
    candidate_value: float | None,
    n_baseline: int,
    n_candidate: int,
    options: CompareOptions,
) -> MetricResult:
    point = _relative(baseline_value, candidate_value)
    return MetricResult(
        name=name,
        unit=unit,
        direction=direction,
        baseline=baseline_value,
        candidate=candidate_value,
        delta_pct=point,
        low_pct=None,
        high_pct=None,
        n_baseline=n_baseline,
        n_candidate=n_candidate,
        source="record",
        verdict=_point_verdict(point, direction, options.max_regression),
    )


def _level_metric_values(
    run: RunData, concurrency: int
) -> tuple[dict[str, Any] | None, tuple[list[float], list[list[float]], int]]:
    return (run.level(concurrency), _samples(run, concurrency))


def _compare_level(
    baseline: RunData,
    candidate: RunData,
    level: int,
    options: CompareOptions,
) -> LevelResult:
    (baseline_level, baseline_samples) = _level_metric_values(baseline, level)
    (candidate_level, candidate_samples) = _level_metric_values(candidate, level)
    baseline_ttft, baseline_itl, baseline_ok = baseline_samples
    candidate_ttft, candidate_itl, candidate_ok = candidate_samples
    metrics: list[MetricResult] = []

    def summary_metric(
        name: str,
        key: str,
        unit: str,
        direction: str,
        percentile_key: str = "",
    ) -> MetricResult:
        baseline_value = _level_number(baseline_level, key, percentile_key)
        candidate_value = _level_number(candidate_level, key, percentile_key)
        return _record_metric(
            name,
            unit,
            direction,
            baseline_value,
            candidate_value,
            baseline_ok,
            candidate_ok,
            options,
        )

    if baseline_ttft and candidate_ttft:
        metrics.append(
            _interval_metric(
                "ttft_p50_ms", "ms", LOWER_IS_BETTER, 50.0, baseline_ttft, candidate_ttft, options
            )
        )
        metrics.append(
            _interval_metric(
                "ttft_p95_ms", "ms", LOWER_IS_BETTER, 95.0, baseline_ttft, candidate_ttft, options
            )
        )
    else:
        for name, percentile_key in (("ttft_p50_ms", "p50"), ("ttft_p95_ms", "p95")):
            metrics.append(
                summary_metric(name, "ttft_ms", "ms", LOWER_IS_BETTER, percentile_key)
            )

    if baseline_itl and candidate_itl:
        metrics.append(
            _cluster_metric(
                "itl_p50_ms", "ms", 50.0, baseline_itl, candidate_itl, options
            )
        )
    else:
        metrics.append(
            summary_metric("itl_p50_ms", "itl_ms", "ms", LOWER_IS_BETTER, "p50")
        )

    metrics.append(
        summary_metric("decode_tokens_s", "decode_tokens_s", "tok/s", HIGHER_IS_BETTER)
    )
    metrics.append(summary_metric("error_rate", "error_rate", "%", LOWER_IS_BETTER))
    return LevelResult(concurrency=level, metrics=tuple(metrics))


def _level_number(
    level: dict[str, Any] | None, key: str, percentile_key: str
) -> float | None:
    if level is None:
        return None
    value = level.get(key)
    if percentile_key:
        if not isinstance(value, dict):
            return None
        nested = value.get(percentile_key)
        return float(nested) if isinstance(nested, (int, float)) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


KERNEL_MIN_SAMPLES = 4


def _row_samples(entry: dict[str, Any]) -> list[float]:
    raw = entry.get("samples")
    if not isinstance(raw, list):
        return []
    values: list[float] = []
    for item in raw:
        if isinstance(item, (int, float)) and not isinstance(item, bool) and item > 0.0:
            values.append(float(item))
    return values


def _kernel_rows(run: RunData) -> dict[str, dict[str, Any]]:
    metrics = run.record.get("metrics")
    if not isinstance(metrics, dict):
        raise InvalidMeasurement(f"{run.run_id}: the record has no metrics object")
    rows: dict[str, dict[str, Any]] = {}
    copy_rows = metrics.get("copy", [])
    if isinstance(copy_rows, list):
        for entry in copy_rows:
            if not isinstance(entry, dict):
                continue
            block = entry.get("block")
            gbps = entry.get("gbps")
            if isinstance(block, bool) or not isinstance(block, int):
                continue
            if not isinstance(gbps, (int, float)) or isinstance(gbps, bool):
                continue
            variant = str(entry.get("variant", ""))
            detail = f"{variant} block {block}" if variant else f"block {block}"
            rows[f"copy {detail}"] = {
                "benchmark": "copy",
                "detail": detail,
                "unit": "GB/s",
                "value": float(gbps),
                "samples": _row_samples(entry),
            }
    gemm_rows = metrics.get("gemm", [])
    if isinstance(gemm_rows, list):
        for entry in gemm_rows:
            if not isinstance(entry, dict):
                continue
            dtype = entry.get("dtype")
            layout = entry.get("layout")
            size = entry.get("size")
            gflops = entry.get("gflops")
            if not isinstance(dtype, str) or not isinstance(layout, str):
                continue
            if isinstance(size, bool) or not isinstance(size, int):
                continue
            if not isinstance(gflops, (int, float)) or isinstance(gflops, bool):
                continue
            detail = f"{dtype} {layout} {size}"
            rows[f"gemm {detail}"] = {
                "benchmark": "gemm",
                "detail": detail,
                "unit": "GFLOPS",
                "value": float(gflops),
                "samples": _row_samples(entry),
            }
    return rows


def _compare_kernel(
    baseline: RunData, candidate: RunData, options: CompareOptions
) -> tuple[tuple[RowResult, ...], tuple[str, ...]]:
    base_rows = _kernel_rows(baseline)
    cand_rows = _kernel_rows(candidate)
    shared = sorted(set(base_rows) & set(cand_rows))
    if not shared:
        raise InvalidMeasurement("the kernel runs share no benchmark row")
    results: list[RowResult] = []
    for key in shared:
        base = base_rows[key]
        cand = cand_rows[key]
        base_samples = base["samples"]
        cand_samples = cand["samples"]
        if base_samples and cand_samples:
            delta = stats.bootstrap_mean_delta_ci(
                base_samples,
                cand_samples,
                resamples=options.resamples,
                confidence=options.confidence,
                seed=options.seed,
            )
            enough = (
                len(base_samples) >= KERNEL_MIN_SAMPLES
                and len(cand_samples) >= KERNEL_MIN_SAMPLES
            )
            verdict = (
                _interval_verdict(
                    delta.point_pct,
                    delta.low_pct,
                    delta.high_pct,
                    HIGHER_IS_BETTER,
                    options.max_regression,
                )
                if enough
                else "inconclusive"
            )
            reason = "" if enough else f"fewer than {KERNEL_MIN_SAMPLES} iterations"
            results.append(
                RowResult(
                    benchmark=str(base["benchmark"]),
                    detail=str(base["detail"]),
                    unit=str(base["unit"]),
                    baseline=float(base["value"]),
                    candidate=float(cand["value"]),
                    delta_pct=delta.point_pct,
                    low_pct=delta.low_pct,
                    high_pct=delta.high_pct,
                    n_baseline=len(base_samples),
                    n_candidate=len(cand_samples),
                    source="samples",
                    verdict=verdict,
                    reason=reason,
                )
            )
        else:
            point = _relative(float(base["value"]), float(cand["value"]))
            results.append(
                RowResult(
                    benchmark=str(base["benchmark"]),
                    detail=str(base["detail"]),
                    unit=str(base["unit"]),
                    baseline=float(base["value"]),
                    candidate=float(cand["value"]),
                    delta_pct=point,
                    low_pct=None,
                    high_pct=None,
                    n_baseline=0,
                    n_candidate=0,
                    source="record",
                    verdict=_point_verdict(point, HIGHER_IS_BETTER, options.max_regression),
                )
            )
    skipped = tuple(sorted(set(base_rows) ^ set(cand_rows)))
    return tuple(results), skipped


def compare_runs(
    baseline: RunData, candidate: RunData, options: CompareOptions | None = None
) -> CompareReport:
    """Compare two bench or kernel runs and return the full report."""
    chosen = options or CompareOptions()
    if baseline.command != candidate.command:
        raise InvalidMeasurement(
            f"cannot compare a {baseline.command} run with a {candidate.command} run"
        )
    compatibility = _compatibility(baseline, candidate, chosen)
    if baseline.command == KERNEL_COMMAND:
        rows, skipped_rows = _compare_kernel(baseline, candidate, chosen)
        return CompareReport(
            baseline=_summary(baseline),
            candidate=_summary(candidate),
            compatibility=compatibility,
            context=_context(baseline, candidate),
            levels=(),
            skipped_levels=(),
            options=chosen,
            command=baseline.command,
            rows=rows,
            skipped_rows=skipped_rows,
        )
    if baseline.command == PROFILE_COMMAND:
        raise InvalidMeasurement(
            "profile runs are inspection evidence; their kernel summaries are not gated"
        )
    if baseline.command != BENCH_COMMAND:
        raise InvalidMeasurement(f"cannot compare {baseline.command} runs")
    baseline_levels = {level["concurrency"] for level in baseline.levels()}
    candidate_levels = {level["concurrency"] for level in candidate.levels()}
    shared = sorted(baseline_levels & candidate_levels)
    if not shared:
        raise InvalidMeasurement(
            "the runs share no concurrency level, baseline "
            f"{sorted(baseline_levels)} against candidate {sorted(candidate_levels)}"
        )
    skipped = tuple(sorted(baseline_levels ^ candidate_levels))
    levels = tuple(_compare_level(baseline, candidate, level, chosen) for level in shared)
    return CompareReport(
        baseline=_summary(baseline),
        candidate=_summary(candidate),
        compatibility=compatibility,
        context=_context(baseline, candidate),
        levels=levels,
        skipped_levels=skipped,
        options=chosen,
        command=baseline.command,
    )


def _observations(report: CompareReport) -> list[tuple[str, str, float | None]]:
    observations = [(metric.name, metric.verdict, metric.delta_pct) for metric in report.metrics()]
    observations.extend((row.label, row.verdict, row.delta_pct) for row in report.rows)
    return observations


def gate_failures(report: CompareReport) -> list[str]:
    """Every reason the gate would fail, in a stable order."""
    failures = list(report.compatibility.failures)
    for label, verdict, delta in _observations(report):
        if verdict == "missing":
            failures.append(f"missing metric {label}")
        elif verdict == "regressed":
            failures.append(
                f"{label}: {_format_pct(delta)} beyond "
                f"{report.options.max_regression:.1f}% tolerance"
            )
        elif verdict == "point_regressed" and report.options.fail_on_point:
            failures.append(f"{label}: point estimate regressed")
        elif verdict == "inconclusive" and report.options.fail_on_inconclusive:
            failures.append(f"{label}: inconclusive")
    return failures


def gate_exit_code(report: CompareReport) -> int:
    """0 for a pass, 1 for a regression, 3 for unusable evidence."""
    if not report.compatibility.ok:
        return 3
    verdicts = [verdict for _, verdict, _ in _observations(report)]
    if any(verdict in {"missing", "invalid"} for verdict in verdicts):
        return 3
    if any(verdict == "regressed" for verdict in verdicts):
        return 1
    if report.options.fail_on_point and any(verdict == "point_regressed" for verdict in verdicts):
        return 1
    if report.options.fail_on_inconclusive and "inconclusive" in verdicts:
        return 1
    return 0


def table_headers(report: CompareReport) -> list[str]:
    if report.command == KERNEL_COMMAND:
        return ["kernel", "detail", "baseline", "candidate", "delta", "interval", "n", "verdict"]
    return ["conc", "metric", "baseline", "candidate", "delta", "interval", "n", "verdict"]


def table_rows(report: CompareReport) -> list[list[str]]:
    """Rows for the terminal table and the markdown report."""
    if report.command == KERNEL_COMMAND:
        return [
            [
                row.benchmark,
                row.detail,
                _format_value(row.baseline, row.unit),
                _format_value(row.candidate, row.unit),
                _format_pct(row.delta_pct),
                _format_interval(row.low_pct, row.high_pct),
                f"{row.n_baseline}/{row.n_candidate}",
                row.verdict,
            ]
            for row in report.rows
        ]
    rows: list[list[str]] = []
    for level in report.levels:
        for metric in level.metrics:
            rows.append(
                [
                    str(level.concurrency),
                    metric.name,
                    _format_value(metric.baseline, metric.unit),
                    _format_value(metric.candidate, metric.unit),
                    _format_pct(metric.delta_pct),
                    _format_interval(metric.low_pct, metric.high_pct),
                    f"{metric.n_baseline}/{metric.n_candidate}",
                    metric.verdict,
                ]
            )
    return rows


def _format_value(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "%":
        return f"{value * 100.0:.2f}%"
    if unit == "ms":
        return f"{value:.1f} ms"
    return f"{value:.1f} {unit}"


def _format_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1f}%"


def _format_interval(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "-"
    return f"[{low:+.1f}, {high:+.1f}]%"


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)
