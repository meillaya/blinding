"""Tuning llama.cpp launch flags against llama-bench.

A grid is cheap to describe and slow to measure, so the two stay apart:
`expand_grid` lists the candidates, `run_candidate` measures one, and the
helpers below decide what the numbers are allowed to claim. The noise rule is
blunt on purpose: a pick needs a gap larger than one standard deviation on the
deciding metric, because a handful of repetitions does not resolve smaller
differences.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypedDict

from blinding.errors import ConfigError, UpstreamError

GIB = 1024**3
DEFAULT_BATCH = 512
DEFAULT_UBATCH = 128
DEFAULT_PP = 512
DEFAULT_TG = 128
DEFAULT_REPS = 3
DEFAULT_TIMEOUT_S = 300.0
MAX_DETAIL = 300
TEST_PATTERN = re.compile(r"^(pp|tg)[0-9]+$")
QUANT_PATTERN = re.compile(
    r"(?:^|[-_.])(i?q[0-9][\w]*|f16|f32|bf16)(?=[-_.]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TuneCandidate:
    """One llama-bench configuration to measure.

    A thread count of zero is forwarded unchanged: llama.cpp treats zero as
    "pick a default", and the JSON it reports back carries the resolved count.
    """

    quant: str
    threads: int
    gpu_layers: int
    batch_size: int = DEFAULT_BATCH
    ubatch_size: int = DEFAULT_UBATCH
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.quant:
            raise ConfigError("candidate quant must not be empty")
        if self.threads < 0:
            raise ConfigError("candidate threads must not be negative")
        if self.gpu_layers < 0:
            raise ConfigError("candidate gpu_layers must not be negative")
        if self.batch_size <= 0 or self.ubatch_size <= 0:
            raise ConfigError("candidate batch sizes must be positive")
        if self.ubatch_size > self.batch_size:
            raise ConfigError("candidate ubatch_size must not exceed batch_size")
        for argument in self.extra_args:
            if not isinstance(argument, str) or not argument:
                raise ConfigError("candidate extra_args must be non-empty strings")


@dataclass(frozen=True, slots=True)
class BenchRow:
    """Throughput for one measured configuration.

    llama-bench prints one object per test, so a row joins the prompt and
    generation objects from one invocation. `test` records the labels that were
    joined ("pp512+tg128"), which keeps an unusual sweep visible in the report.
    """

    quant: str
    test: str
    threads: int
    gpu_layers: int
    pp_ts: float
    tg_ts: float
    pp_stddev: float
    tg_stddev: float
    model_size: int

    def as_dict(self) -> dict[str, object]:
        """Plain mapping for the run record."""
        return {
            "quant": self.quant,
            "test": self.test,
            "threads": self.threads,
            "gpu_layers": self.gpu_layers,
            "pp_ts": round(self.pp_ts, 3),
            "tg_ts": round(self.tg_ts, 3),
            "pp_stddev": round(self.pp_stddev, 3),
            "tg_stddev": round(self.tg_stddev, 3),
            "model_size": self.model_size,
        }


def expand_grid(
    quants: tuple[str, ...],
    threads: tuple[int, ...],
    gpu_layers: tuple[int, ...],
) -> list[TuneCandidate]:
    """Every unique candidate in the grid, in input order.

    Duplicates are common in hand written flag lists (two adjacent core counts
    often land on the same configuration), and measuring one twice spends the
    slowest part of the run for nothing.
    """
    candidates: list[TuneCandidate] = []
    seen: set[TuneCandidate] = set()
    for quant in quants:
        for thread_count in threads:
            for layer_count in gpu_layers:
                candidate = TuneCandidate(
                    quant=quant,
                    threads=thread_count,
                    gpu_layers=layer_count,
                )
                if candidate in seen:
                    continue
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


@dataclass(slots=True)
class _Group:
    """Accumulator for the test objects that share one configuration."""

    filename: str
    threads: int
    gpu_layers: int
    model_size: int
    tests: list[str] = field(default_factory=list)
    pp_ts: float = 0.0
    pp_stddev: float = 0.0
    tg_ts: float = 0.0
    tg_stddev: float = 0.0


def _guess_quant(filename: str) -> str:
    """Best effort quant from a GGUF filename; run_candidate overwrites it."""
    match = QUANT_PATTERN.search(Path(filename).stem)
    return match.group(1) if match is not None else ""


def _require_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise UpstreamError(f"llama-bench JSON needs a string {key}")
    return value


def _require_int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UpstreamError(f"llama-bench JSON needs an integer {key}")
    return value


def _require_float(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UpstreamError(f"llama-bench JSON needs a number {key}")
    return float(value)


def _optional_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    """Integer when the build prints it, `default` when it does not.

    Current llama-bench builds drop some flags (`-b`, `-ub`) from the JSON even
    though the invocation accepted them, so those keys cannot be required.
    """
    if key not in raw:
        return default
    return _require_int(raw, key)


def _test_label(raw: Mapping[str, Any], prompt_tokens: int, generated_tokens: int) -> str:
    """Test label from the JSON, derived from token counts when it is absent.

    Older llama-bench builds print a `test` string ("pp512", "tg128"). Build
    b29c606 prints the two counts instead: a prompt test has zero generated
    tokens, and a generation test has zero prompt tokens.
    """
    if "test" in raw:
        value = raw["test"]
        if not isinstance(value, str) or TEST_PATTERN.match(value) is None:
            raise UpstreamError(f"unknown llama-bench test {value!r}")
        return value
    if prompt_tokens > 0 and generated_tokens == 0:
        return f"pp{prompt_tokens}"
    if generated_tokens > 0 and prompt_tokens == 0:
        return f"tg{generated_tokens}"
    raise UpstreamError(
        "llama-bench JSON has no test label and no clear prompt or generation counts"
    )


def parse_bench_json(text: str) -> list[BenchRow]:
    """Turn llama-bench JSON output into one row per configuration.

    The grouping key is every reported setting that changes the workload: two
    thread counts belong in two rows even when they share a model. A JSON list
    that holds one prompt test and one generation test for the same flags
    becomes one row with both throughputs.
    """
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UpstreamError(f"llama-bench did not print valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise UpstreamError("llama-bench JSON must be a list of test objects")

    groups: dict[tuple[str, int, int, int, int], _Group] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            raise UpstreamError("llama-bench JSON entries must be objects")
        raw: dict[str, Any] = {str(key): value for key, value in entry.items()}
        test = _test_label(
            raw,
            _optional_int(raw, "n_prompt", 0),
            _optional_int(raw, "n_gen", 0),
        )
        key = (
            _require_str(raw, "model_filename"),
            _optional_int(raw, "n_batch", 0),
            _optional_int(raw, "n_ubatch", 0),
            _require_int(raw, "n_threads"),
            _require_int(raw, "n_gpu_layers"),
        )
        group = groups.get(key)
        if group is None:
            group = _Group(
                filename=key[0],
                threads=key[3],
                gpu_layers=key[4],
                model_size=_require_int(raw, "model_size"),
            )
            groups[key] = group
        if test not in group.tests:
            group.tests.append(test)
        average = _require_float(raw, "avg_ts")
        deviation = _require_float(raw, "stddev_ts")
        if test.startswith("pp"):
            group.pp_ts = average
            group.pp_stddev = deviation
        else:
            group.tg_ts = average
            group.tg_stddev = deviation

    return [
        BenchRow(
            quant=_guess_quant(group.filename),
            test="+".join(group.tests),
            threads=group.threads,
            gpu_layers=group.gpu_layers,
            pp_ts=group.pp_ts,
            tg_ts=group.tg_ts,
            pp_stddev=group.pp_stddev,
            tg_stddev=group.tg_stddev,
            model_size=group.model_size,
        )
        for group in groups.values()
    ]


def _excerpt(text: str) -> str:
    """Collapse a failing command's output to one short line for the error."""
    return " ".join(text.split())[:MAX_DETAIL]


def run_candidate(
    bench_binary: str,
    model_path: Path,
    candidate: TuneCandidate,
    *,
    pp: int = DEFAULT_PP,
    tg: int = DEFAULT_TG,
    reps: int = DEFAULT_REPS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> BenchRow:
    """Measure one candidate with llama-bench and return the merged row."""
    if pp <= 0 or tg <= 0 or reps <= 0:
        raise ConfigError("pp, tg, and reps must be positive")
    if timeout_s <= 0:
        raise ConfigError("timeout_s must be positive")
    argv = [
        bench_binary,
        "-m",
        str(model_path),
        "-t",
        str(candidate.threads),
        "-ngl",
        str(candidate.gpu_layers),
        "-b",
        str(candidate.batch_size),
        "-ub",
        str(candidate.ubatch_size),
        "-p",
        str(pp),
        "-n",
        str(tg),
        "-r",
        str(reps),
        "-o",
        "json",
        *candidate.extra_args,
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpstreamError(f"llama-bench exceeded {timeout_s:g}s on {candidate.quant}") from exc
    except OSError as exc:
        raise UpstreamError(f"cannot run llama-bench at {bench_binary!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = _excerpt((completed.stderr or "") + " " + (completed.stdout or ""))
        raise UpstreamError(f"llama-bench failed with exit code {completed.returncode}: {detail}")
    rows = parse_bench_json(completed.stdout or "")
    if len(rows) != 1:
        raise UpstreamError(f"llama-bench reported {len(rows)} configurations for one candidate")
    return replace(rows[0], quant=candidate.quant)


def pareto(rows: Sequence[BenchRow]) -> list[BenchRow]:
    """Rows that no other row beats on both prompt and generation throughput."""
    frontier: list[BenchRow] = []
    for row in rows:
        dominated = any(
            other is not row
            and other.pp_ts >= row.pp_ts
            and other.tg_ts >= row.tg_ts
            and (other.pp_ts > row.pp_ts or other.tg_ts > row.tg_ts)
            for other in rows
        )
        if not dominated:
            frontier.append(row)
    return frontier


class TunePick(TypedDict):
    """One recommended configuration and the metric that decided it."""

    quant: str
    threads: int
    gpu_layers: int
    metric: str
    mean: float
    stddev: float


class Recommendation(TypedDict):
    """The two picks, plus the runner-up whenever the margin is inside noise."""

    interactive: TunePick | None
    prompt_heavy: TunePick | None
    noise_tie: bool
    interactive_runner_up: TunePick | None
    prompt_heavy_runner_up: TunePick | None


def _pp_ts(row: BenchRow) -> float:
    return row.pp_ts


def _tg_ts(row: BenchRow) -> float:
    return row.tg_ts


def _pp_stddev(row: BenchRow) -> float:
    return row.pp_stddev


def _tg_stddev(row: BenchRow) -> float:
    return row.tg_stddev


def _decide(
    rows: Sequence[BenchRow],
    metric: Callable[[BenchRow], float],
    deviation: Callable[[BenchRow], float],
) -> tuple[BenchRow | None, BenchRow | None]:
    """Best row, and the runner-up when the margin is inside the noise band."""
    if not rows:
        return (None, None)
    ordered = sorted(rows, key=metric, reverse=True)
    best = ordered[0]
    if len(ordered) == 1:
        return (best, None)
    second = ordered[1]
    if metric(best) - metric(second) > max(deviation(best), deviation(second)):
        return (best, None)
    return (best, second)


def _pick_row(
    row: BenchRow | None,
    metric: str,
    value: Callable[[BenchRow], float],
    deviation: Callable[[BenchRow], float],
) -> TunePick | None:
    if row is None:
        return None
    return TunePick(
        quant=row.quant,
        threads=row.threads,
        gpu_layers=row.gpu_layers,
        metric=metric,
        mean=round(value(row), 3),
        stddev=round(deviation(row), 3),
    )


def recommend(rows: Sequence[BenchRow]) -> Recommendation:
    """Pick one configuration per workload, refusing margins inside noise.

    Interactive use is decided by generation throughput and prompt heavy use by
    prompt processing throughput. Rows with a zero metric carry no evidence for
    either choice and are left out before ranking.
    """
    complete = [row for row in rows if row.pp_ts > 0.0 and row.tg_ts > 0.0]
    interactive, interactive_runner = _decide(complete, _tg_ts, _tg_stddev)
    prompt_heavy, prompt_runner = _decide(complete, _pp_ts, _pp_stddev)
    return Recommendation(
        interactive=_pick_row(interactive, "tg_ts", _tg_ts, _tg_stddev),
        prompt_heavy=_pick_row(prompt_heavy, "pp_ts", _pp_ts, _pp_stddev),
        noise_tie=interactive_runner is not None or prompt_runner is not None,
        interactive_runner_up=_pick_row(interactive_runner, "tg_ts", _tg_ts, _tg_stddev),
        prompt_heavy_runner_up=_pick_row(prompt_runner, "pp_ts", _pp_ts, _pp_stddev),
    )


def _threads_label(threads: int) -> str:
    return "auto" if threads == 0 else str(threads)


def summary_rows(rows: Sequence[BenchRow]) -> list[list[str]]:
    """Table rows for the report and the terminal."""
    table: list[list[str]] = []
    for row in rows:
        table.append(
            [
                row.quant,
                _threads_label(row.threads),
                str(row.gpu_layers),
                f"{row.pp_ts:.1f}",
                f"{row.tg_ts:.1f}",
                f"{row.model_size / GIB:.2f}",
            ]
        )
    return table


def _describe_pick(pick: TunePick) -> str:
    return (
        f"{pick['quant']} threads={_threads_label(pick['threads'])} gpu_layers={pick['gpu_layers']}"
    )


def _metric_unit(pick: TunePick) -> str:
    return "tg" if pick["metric"] == "tg_ts" else "pp"


def _pick_lines(
    label: str,
    pick: TunePick | None,
    runner_up: TunePick | None,
) -> list[str]:
    if pick is None:
        return [f"{label}: no complete row"]
    lines = [
        f"{label}: {_describe_pick(pick)} "
        f"{_metric_unit(pick)} {pick['mean']:.1f} +/- {pick['stddev']:.1f} tok/s"
    ]
    if runner_up is not None:
        lines.append(
            f"  runner-up: {_describe_pick(runner_up)} "
            f"{_metric_unit(runner_up)} {runner_up['mean']:.1f} "
            f"+/- {runner_up['stddev']:.1f} tok/s"
        )
    return lines


def render_text(rows: Sequence[BenchRow], recommendation: Recommendation) -> str:
    """Human-readable tuning block for the terminal and the report."""
    if not rows:
        return "no tuning rows"
    frontier = pareto(rows)
    lines = [
        f"tuning recommendation from {len(rows)} configurations",
        f"pareto frontier: {len(frontier)} of {len(rows)} rows",
    ]
    lines.extend(
        _pick_lines(
            "interactive",
            recommendation["interactive"],
            recommendation["interactive_runner_up"],
        )
    )
    lines.extend(
        _pick_lines(
            "prompt heavy",
            recommendation["prompt_heavy"],
            recommendation["prompt_heavy_runner_up"],
        )
    )
    if recommendation["noise_tie"]:
        lines.append(
            "noise tie: at least one pick is inside one standard deviation of its "
            "runner-up, so measure more repetitions before choosing."
        )
    return "\n".join(lines)
