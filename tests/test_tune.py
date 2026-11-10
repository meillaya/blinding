"""Tests for the llama-bench grid, parser, Pareto set, and recommendation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from blinding import tune
from blinding.errors import ConfigError, UpstreamError

MODEL_FILENAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
MODEL_SIZE = 1 << 30


def _entry(
    test: str,
    *,
    avg_ts: float,
    stddev_ts: float,
    threads: int = 8,
    gpu_layers: int = 0,
    batch: int = 512,
    ubatch: int = 128,
    filename: str = MODEL_FILENAME,
    model_size: int = MODEL_SIZE,
) -> dict[str, object]:
    """One llama-bench test object, shaped like a real `-o json` entry."""
    return {
        "build_commit": "deadbeef",
        "model_filename": filename,
        "model_size": model_size,
        "model_n_params": 494_000_000,
        "n_batch": batch,
        "n_ubatch": ubatch,
        "n_threads": threads,
        "n_gpu_layers": gpu_layers,
        "test": test,
        "n_prompt": 512,
        "n_gen": 128,
        "avg_ns": 1_000_000,
        "stddev_ns": 10_000,
        "avg_ts": avg_ts,
        "stddev_ts": stddev_ts,
    }


def _real_entry(
    *,
    prompt: int,
    gen: int,
    avg_ts: float,
    stddev_ts: float,
    threads: int = 8,
    gpu_layers: int = 0,
) -> dict[str, object]:
    """One entry as llama-bench b29c606 prints it: no test, batch, or ubatch keys."""
    return {
        "build_commit": "b29c606",
        "build_number": 7000,
        "model_filename": MODEL_FILENAME,
        "model_size": MODEL_SIZE,
        "model_n_params": 494_000_000,
        "n_threads": threads,
        "n_gpu_layers": gpu_layers,
        "n_prompt": prompt,
        "n_gen": gen,
        "avg_ns": 1_000_000,
        "stddev_ns": 10_000,
        "avg_ts": avg_ts,
        "stddev_ts": stddev_ts,
        "backends": "CPU",
    }


def _payload(*entries: dict[str, object]) -> str:
    return json.dumps(list(entries))


def _candidate(*, threads: int = 8, gpu_layers: int = 0) -> tune.TuneCandidate:
    return tune.TuneCandidate(quant="q4_k_m", threads=threads, gpu_layers=gpu_layers)


def _row(
    *,
    pp_ts: float,
    tg_ts: float,
    pp_stddev: float = 1.0,
    tg_stddev: float = 1.0,
    threads: int = 8,
    gpu_layers: int = 0,
    test: str = "pp512+tg128",
    model_size: int = MODEL_SIZE,
) -> tune.BenchRow:
    return tune.BenchRow(
        quant="q4_k_m",
        test=test,
        threads=threads,
        gpu_layers=gpu_layers,
        pp_ts=pp_ts,
        tg_ts=tg_ts,
        pp_stddev=pp_stddev,
        tg_stddev=tg_stddev,
        model_size=model_size,
    )


def test_expand_grid_keeps_input_order_and_allows_auto_threads() -> None:
    candidates = tune.expand_grid(("q4_k_m", "q8_0"), (0, 8), (0, 99))
    assert len(candidates) == 8
    assert len(set(candidates)) == 8
    assert candidates[0] == tune.TuneCandidate(quant="q4_k_m", threads=0, gpu_layers=0)
    assert candidates[0].batch_size == tune.DEFAULT_BATCH
    assert candidates[0].ubatch_size == tune.DEFAULT_UBATCH
    assert candidates[0].extra_args == ()
    assert candidates[-1] == tune.TuneCandidate(quant="q8_0", threads=8, gpu_layers=99)
    assert tune.expand_grid((), (8,), (0,)) == []


def test_expand_grid_dedupes_repeated_axis_values() -> None:
    candidates = tune.expand_grid(("q4_k_m", "q4_k_m"), (4, 4), (0,))
    assert candidates == [tune.TuneCandidate(quant="q4_k_m", threads=4, gpu_layers=0)]


@pytest.mark.parametrize(
    ("quants", "threads", "gpu_layers"),
    [
        (("q4_k_m",), (-1,), (0,)),
        (("q4_k_m",), (8,), (-1,)),
        (("",), (8,), (0,)),
    ],
)
def test_expand_grid_rejects_impossible_values(
    quants: tuple[str, ...],
    threads: tuple[int, ...],
    gpu_layers: tuple[int, ...],
) -> None:
    with pytest.raises(ConfigError):
        tune.expand_grid(quants, threads, gpu_layers)


def test_parse_bench_json_merges_pp_and_tg() -> None:
    payload = _payload(
        _entry("pp512", avg_ts=150.0, stddev_ts=5.0),
        _entry("tg128", avg_ts=40.0, stddev_ts=2.0),
    )
    rows = tune.parse_bench_json(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row.quant == "q4_k_m"
    assert row.test == "pp512+tg128"
    assert row.pp_ts == 150.0
    assert row.tg_ts == 40.0
    assert row.pp_stddev == 5.0
    assert row.tg_stddev == 2.0
    assert row.threads == 8
    assert row.gpu_layers == 0
    assert row.model_size == MODEL_SIZE


def test_parse_bench_json_derives_labels_from_token_counts() -> None:
    payload = _payload(
        _real_entry(prompt=512, gen=0, avg_ts=150.0, stddev_ts=5.0),
        _real_entry(prompt=0, gen=128, avg_ts=40.0, stddev_ts=2.0),
    )
    rows = tune.parse_bench_json(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row.test == "pp512+tg128"
    assert row.pp_ts == 150.0
    assert row.tg_ts == 40.0
    assert row.pp_stddev == 5.0
    assert row.tg_stddev == 2.0
    assert row.threads == 8
    assert row.gpu_layers == 0
    assert row.model_size == MODEL_SIZE


def test_parse_bench_json_rejects_an_unlabelled_mixed_object() -> None:
    entry = _real_entry(prompt=512, gen=128, avg_ts=1.0, stddev_ts=0.1)
    with pytest.raises(UpstreamError):
        tune.parse_bench_json(_payload(entry))


def test_parse_bench_json_keeps_configurations_apart() -> None:
    payload = _payload(
        _entry("pp512", avg_ts=150.0, stddev_ts=5.0),
        _entry("tg128", avg_ts=40.0, stddev_ts=2.0),
        _entry("pp512", avg_ts=120.0, stddev_ts=4.0, threads=16),
        _entry("tg128", avg_ts=35.0, stddev_ts=1.5, threads=16),
    )
    rows = tune.parse_bench_json(payload)
    assert [(row.threads, row.pp_ts, row.tg_ts) for row in rows] == [
        (8, 150.0, 40.0),
        (16, 120.0, 35.0),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "{}",
        json.dumps([{"test": "xx512"}]),
        json.dumps([{"test": "pp512"}]),
    ],
)
def test_parse_bench_json_rejects_bad_input(payload: str) -> None:
    with pytest.raises(UpstreamError):
        tune.parse_bench_json(payload)


def test_run_candidate_builds_argv_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    payload = _payload(
        _real_entry(prompt=512, gen=0, avg_ts=150.0, stddev_ts=5.0),
        _real_entry(prompt=0, gen=128, avg_ts=40.0, stddev_ts=2.0),
    )

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    monkeypatch.setattr(tune.subprocess, "run", fake_run)
    candidate = tune.TuneCandidate(
        quant="q4_k_m",
        threads=8,
        gpu_layers=0,
        extra_args=("--flash-attn", "on"),
    )
    row = tune.run_candidate(
        "llama-bench",
        Path("/models/tiny.gguf"),
        candidate,
        pp=256,
        tg=64,
        reps=2,
        timeout_s=12.5,
    )
    argv = cast(list[str], captured["argv"])
    assert argv == [
        "llama-bench",
        "-m",
        "/models/tiny.gguf",
        "-t",
        "8",
        "-ngl",
        "0",
        "-b",
        "512",
        "-ub",
        "128",
        "-p",
        "256",
        "-n",
        "64",
        "-r",
        "2",
        "-o",
        "json",
        "--flash-attn",
        "on",
    ]
    kwargs = cast(dict[str, object], captured["kwargs"])
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 12.5
    assert kwargs["check"] is False
    assert row.quant == "q4_k_m"
    assert row.pp_ts == 150.0
    assert row.tg_ts == 40.0


def test_run_candidate_raises_on_failed_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="bench exploded")

    monkeypatch.setattr(tune.subprocess, "run", fake_run)
    with pytest.raises(UpstreamError, match="bench exploded"):
        tune.run_candidate("llama-bench", Path("/models/tiny.gguf"), _candidate())


def test_run_candidate_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 300.0)

    monkeypatch.setattr(tune.subprocess, "run", fake_run)
    with pytest.raises(UpstreamError, match="exceeded"):
        tune.run_candidate("llama-bench", Path("/models/tiny.gguf"), _candidate())


def test_run_candidate_raises_on_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(tune.subprocess, "run", fake_run)
    with pytest.raises(UpstreamError, match="cannot run llama-bench"):
        tune.run_candidate("llama-bench", Path("/models/tiny.gguf"), _candidate())


def test_pareto_drops_dominated_rows() -> None:
    fast_prompt = _row(pp_ts=150.0, tg_ts=30.0)
    fast_generation = _row(pp_ts=100.0, tg_ts=45.0)
    dominated = _row(pp_ts=90.0, tg_ts=25.0)
    assert tune.pareto([fast_prompt, fast_generation, dominated]) == [
        fast_prompt,
        fast_generation,
    ]


def test_recommend_picks_a_clear_winner() -> None:
    balanced = _row(pp_ts=100.0, tg_ts=50.0, pp_stddev=2.0, tg_stddev=2.0, threads=8)
    prompt_fast = _row(
        pp_ts=150.0,
        tg_ts=40.0,
        pp_stddev=3.0,
        tg_stddev=1.0,
        threads=16,
    )
    recommendation = tune.recommend([balanced, prompt_fast])
    assert recommendation["noise_tie"] is False
    interactive = recommendation["interactive"]
    prompt_heavy = recommendation["prompt_heavy"]
    assert interactive is not None
    assert interactive["threads"] == 8
    assert interactive["metric"] == "tg_ts"
    assert interactive["mean"] == 50.0
    assert recommendation["interactive_runner_up"] is None
    assert prompt_heavy is not None
    assert prompt_heavy["threads"] == 16
    assert prompt_heavy["metric"] == "pp_ts"
    assert prompt_heavy["mean"] == 150.0
    assert recommendation["prompt_heavy_runner_up"] is None


def test_recommend_names_both_inside_noise() -> None:
    first = _row(pp_ts=100.0, tg_ts=50.0, pp_stddev=5.0, tg_stddev=5.0, threads=8)
    second = _row(pp_ts=98.0, tg_ts=47.0, pp_stddev=4.0, tg_stddev=6.0, threads=16)
    recommendation = tune.recommend([first, second])
    assert recommendation["noise_tie"] is True
    interactive_runner = recommendation["interactive_runner_up"]
    prompt_runner = recommendation["prompt_heavy_runner_up"]
    assert interactive_runner is not None
    assert interactive_runner["threads"] == 16
    assert prompt_runner is not None
    assert prompt_runner["threads"] == 16
    text = tune.render_text([first, second], recommendation)
    assert "runner-up" in text
    assert "threads=8" in text
    assert "threads=16" in text
    assert "noise tie" in text


def test_recommend_ignores_incomplete_rows() -> None:
    zero = _row(pp_ts=0.0, tg_ts=0.0, threads=4)
    incomplete = _row(pp_ts=500.0, tg_ts=0.0, threads=2)
    solid = _row(pp_ts=100.0, tg_ts=50.0, threads=8)
    recommendation = tune.recommend([zero, incomplete, solid])
    assert recommendation["interactive"] is not None
    assert recommendation["interactive"]["threads"] == 8
    assert recommendation["noise_tie"] is False
    assert recommendation["interactive_runner_up"] is None
    empty = tune.recommend([])
    assert empty["interactive"] is None
    assert empty["prompt_heavy"] is None
    assert empty["noise_tie"] is False


def test_summary_rows_format_metrics() -> None:
    row = _row(pp_ts=123.456, tg_ts=45.678, threads=0, model_size=2 * tune.GIB)
    assert tune.summary_rows([row]) == [["q4_k_m", "auto", "0", "123.5", "45.7", "2.00"]]


def test_bench_row_as_dict_is_json_ready() -> None:
    row = _row(pp_ts=1.23456, tg_ts=2.34567)
    data = row.as_dict()
    assert data["quant"] == "q4_k_m"
    assert data["threads"] == 8
    assert data["pp_ts"] == 1.235
    json.dumps(data)


def test_render_text_without_rows() -> None:
    assert tune.render_text([], tune.recommend([])) == "no tuning rows"
