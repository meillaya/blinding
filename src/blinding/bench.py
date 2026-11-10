"""Closed-loop benchmark over the streaming client.

A level is a fixed number of requests with a fixed number in flight. Throughput
is computed from completed work only. A level whose error rate or sample count
misses the threshold marks the whole run invalid; the CLI then exits non-zero
and refuses to print a summary row.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from blinding import client, stats

PROMPT_SEED = (
    "The machine sits on a desk and the fan noise rises and falls with the load. "
    "Every request asks the same model for a short answer, and every answer "
    "returns a little more slowly as the queue fills. Measure the queue, not the "
    "wish. "
)
DEFAULT_PROMPT_TOKENS = 256
DEFAULT_MAX_TOKENS = 128
DEFAULT_REQUESTS = 8
DEFAULT_WARMUP = 2
DEFAULT_MIN_SUCCESSES = 8
DEFAULT_MAX_ERROR_RATE = 0.05
MAX_PROMPT_REPEATS = 64


@dataclass(frozen=True, slots=True)
class BenchConfig:
    """Everything one benchmark run needs."""

    model: str
    base_url: str
    api_key: str | None
    concurrency: tuple[int, ...]
    requests_per_level: int = DEFAULT_REQUESTS
    warmup: int = DEFAULT_WARMUP
    prompt_tokens: int = DEFAULT_PROMPT_TOKENS
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.0
    timeout_s: float = 180.0
    min_successes: int = DEFAULT_MIN_SUCCESSES
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE


@dataclass(slots=True)
class BenchResult:
    """Aggregate levels, raw samples, and the validity verdict."""

    levels: list[dict[str, Any]] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    valid: bool = False
    reasons: list[str] = field(default_factory=list)
    prompt_tokens: int | None = None


async def build_prompt(config: BenchConfig) -> tuple[str, int | None]:
    """Repeat seed text until it reaches the requested token target."""
    measured = await client.tokenize(
        base_url=config.base_url,
        text=PROMPT_SEED,
        api_key=config.api_key,
    )
    if measured is None or measured <= 0:
        repeats = max(1, min(MAX_PROMPT_REPEATS, config.prompt_tokens // 40))
        text = PROMPT_SEED * repeats
        return (text, None)
    repeats = max(1, min(MAX_PROMPT_REPEATS, -(-config.prompt_tokens // measured)))
    text = PROMPT_SEED * repeats
    actual = await client.tokenize(
        base_url=config.base_url,
        text=text,
        api_key=config.api_key,
    )
    return (text, actual)


async def _one_request(
    config: BenchConfig,
    prompt: str,
    semaphore: asyncio.Semaphore,
) -> client.StreamSample:
    async with semaphore:
        return await client.stream_chat(
            base_url=config.base_url,
            model=config.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            api_key=config.api_key,
            timeout_s=config.timeout_s,
        )


async def _run_level(
    config: BenchConfig,
    prompt: str,
    level: int,
    result: BenchResult,
) -> None:
    semaphore = asyncio.Semaphore(level)
    started = time.perf_counter()
    samples = await asyncio.gather(
        *[_one_request(config, prompt, semaphore) for _ in range(config.requests_per_level)]
    )
    wall_s = time.perf_counter() - started

    ok_samples = [sample for sample in samples if sample.ok]
    errors = len(samples) - len(ok_samples)
    ttfts = [sample.ttft_ms for sample in ok_samples]
    itls = [value for sample in ok_samples for value in sample.itl_ms]
    completion_tokens = sum(sample.completion_tokens for sample in ok_samples)
    request_rate = len(ok_samples) / wall_s if wall_s > 0 else 0.0
    token_rate = completion_tokens / wall_s if wall_s > 0 else 0.0

    level_record: dict[str, Any] = {
        "concurrency": level,
        "requests": len(samples),
        "ok": len(ok_samples),
        "errors": errors,
        "error_rate": round(errors / len(samples), 4) if samples else 1.0,
        "wall_s": round(wall_s, 3),
        "request_rate": round(request_rate, 3),
        "completion_tokens": completion_tokens,
        "decode_tokens_s": round(token_rate, 3),
        "ttft_ms": stats.summarize(ttfts).as_dict() if ttfts else None,
        "itl_ms": stats.summarize(itls).as_dict() if itls else None,
        "latency_ms": stats.summarize([s.latency_ms for s in ok_samples]).as_dict()
        if ok_samples
        else None,
    }
    result.levels.append(level_record)
    for sample in samples:
        record = sample.as_dict()
        record["concurrency"] = level
        result.samples.append(record)


def _validate(config: BenchConfig, result: BenchResult) -> None:
    reasons: list[str] = []
    for level in result.levels:
        requests = int(level["requests"])
        errors = int(level["errors"])
        ok = int(level["ok"])
        if requests == 0:
            reasons.append("a level ran no requests")
            continue
        if errors / requests > config.max_error_rate:
            reasons.append(
                f"concurrency {level['concurrency']}: error rate "
                f"{errors / requests:.2%} above {config.max_error_rate:.2%}"
            )
        if ok < config.min_successes:
            reasons.append(
                f"concurrency {level['concurrency']}: {ok} successes, need {config.min_successes}"
            )
        if level["ttft_ms"] is None or level["itl_ms"] is None:
            reasons.append(f"concurrency {level['concurrency']}: no content tokens")
    result.valid = not reasons
    result.reasons = reasons


async def run_bench(config: BenchConfig) -> BenchResult:
    """Run the warmup and every level, then validate the result."""
    if not config.concurrency or any(level < 1 for level in config.concurrency):
        raise ValueError("concurrency levels must be positive")
    prompt, prompt_tokens = await build_prompt(config)
    result = BenchResult(prompt_tokens=prompt_tokens)
    warmup = asyncio.Semaphore(1)
    for _ in range(max(0, config.warmup)):
        await _one_request(config, prompt, warmup)
    for level in config.concurrency:
        await _run_level(config, prompt, level, result)
    _validate(config, result)
    return result


def summary_rows(result: BenchResult) -> list[list[str]]:
    """Table rows for the report and the terminal."""
    rows: list[list[str]] = []
    for level in result.levels:
        ttft = level["ttft_ms"] or {}
        itl = level["itl_ms"] or {}
        latency = level["latency_ms"] or {}
        rows.append(
            [
                str(level["concurrency"]),
                str(level["ok"]),
                f"{level['error_rate']:.2%}",
                f"{ttft.get('p50', 0):.0f}",
                f"{ttft.get('p95', 0):.0f}",
                f"{itl.get('p50', 0):.0f}",
                f"{level['decode_tokens_s']:.1f}",
                f"{latency.get('p95', 0):.0f}",
            ]
        )
    return rows
