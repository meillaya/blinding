"""Streaming client for the llama.cpp OpenAI surface.

The benchmark needs arrival times, not the server's own averages: the first
content chunk marks TTFT, and each following chunk marks one inter-token step.
The client records both and leaves token counts to the server's usage block
when it sends one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

CHAT_PATH = "/v1/chat/completions"
COMPLETION_PATH = "/v1/completions"
TOKENIZE_PATH = "/tokenize"
MODELS_PATH = "/v1/models"
DEFAULT_TIMEOUT_S = 180.0
STREAM_READ_TIMEOUT_S = 300.0


@dataclass(frozen=True, slots=True)
class StreamSample:
    """One streamed request and the timings taken from it."""

    ok: bool
    status: int
    text: str
    ttft_ms: float
    itl_ms: tuple[float, ...]
    latency_ms: float
    completion_tokens: int
    prompt_tokens: int | None
    chunks: int
    error: str

    def as_dict(self) -> dict[str, object]:
        """Plain mapping, without the generated text."""
        return {
            "ok": self.ok,
            "status": self.status,
            "ttft_ms": round(self.ttft_ms, 3),
            "itl_ms": [round(value, 3) for value in self.itl_ms],
            "latency_ms": round(self.latency_ms, 3),
            "completion_tokens": self.completion_tokens,
            "prompt_tokens": self.prompt_tokens,
            "chunks": self.chunks,
            "error": self.error,
        }


@dataclass(slots=True)
class _StreamState:
    first_at: float = 0.0
    chunk_times: list[float] = field(default_factory=list)
    pieces: list[str] = field(default_factory=list)
    usage_completion: int | None = None
    usage_prompt: int | None = None
    done: bool = False


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _usage_counts(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return (None, None)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        int(prompt) if isinstance(prompt, int) else None,
        int(completion) if isinstance(completion, int) else None,
    )


def _timing_counts(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    timings = payload.get("timings")
    if not isinstance(timings, dict):
        return (None, None)
    prompt = timings.get("prompt_n")
    predicted = timings.get("predicted_n")
    return (
        int(prompt) if isinstance(prompt, int) else None,
        int(predicted) if isinstance(predicted, int) else None,
    )


def _consume_event(state: _StreamState, raw: str, now: float) -> None:
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    usage_prompt, usage_completion = _usage_counts(payload)
    if usage_prompt is not None:
        state.usage_prompt = usage_prompt
    if usage_completion is not None:
        state.usage_completion = usage_completion
    timing_prompt, timing_completion = _timing_counts(payload)
    if state.usage_prompt is None and timing_prompt is not None:
        state.usage_prompt = timing_prompt
    if state.usage_completion is None and timing_completion is not None:
        state.usage_completion = timing_completion
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return
    choice = choices[0]
    if not isinstance(choice, dict):
        return
    delta = choice.get("delta")
    content = ""
    if isinstance(delta, dict):
        raw_content = delta.get("content")
        if isinstance(raw_content, str):
            content = raw_content
    if not content:
        return
    if state.first_at == 0.0:
        state.first_at = now
    state.chunk_times.append(now)
    state.pieces.append(content)


async def stream_chat(
    *,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float = 0.0,
    api_key: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> StreamSample:
    """Send one streamed chat request and time its content chunks."""
    started = time.perf_counter()
    state = _StreamState()
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    timeout = httpx.Timeout(timeout_s, read=STREAM_READ_TIMEOUT_S)
    try:
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream(
                "POST",
                base_url.rstrip("/") + CHAT_PATH,
                json=body,
                headers=_headers(api_key),
            ) as response,
        ):
            status = response.status_code
            if status != httpx.codes.OK:
                detail = (await response.aread()).decode("utf-8", errors="replace")
                ended = time.perf_counter()
                return StreamSample(
                    ok=False,
                    status=status,
                    text="",
                    ttft_ms=0.0,
                    itl_ms=(),
                    latency_ms=(ended - started) * 1000.0,
                    completion_tokens=0,
                    prompt_tokens=None,
                    chunks=0,
                    error=f"HTTP {status}: {detail[:200]}",
                )
            async for line in response.aiter_lines():
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                data = stripped[5:].strip()
                if data == "[DONE]":
                    state.done = True
                    continue
                _consume_event(state, data, time.perf_counter())
    except httpx.HTTPError as exc:
        ended = time.perf_counter()
        return StreamSample(
            ok=False,
            status=0,
            text="",
            ttft_ms=0.0,
            itl_ms=(),
            latency_ms=(ended - started) * 1000.0,
            completion_tokens=0,
            prompt_tokens=None,
            chunks=0,
            error=f"{type(exc).__name__}: {exc}",
        )

    ended = time.perf_counter()
    itl = tuple(
        (later - earlier) * 1000.0
        for earlier, later in zip(state.chunk_times, state.chunk_times[1:], strict=False)
    )
    completion_tokens = state.usage_completion or len(state.chunk_times)
    ok = bool(state.chunk_times) and completion_tokens > 0 and state.done
    error = ""
    if not state.chunk_times:
        error = "stream carried no content"
    elif not state.done:
        error = "stream ended before [DONE]"
    return StreamSample(
        ok=ok,
        status=200,
        text="".join(state.pieces),
        ttft_ms=(state.first_at - started) * 1000.0 if state.first_at else 0.0,
        itl_ms=itl,
        latency_ms=(ended - started) * 1000.0,
        completion_tokens=completion_tokens,
        prompt_tokens=state.usage_prompt,
        chunks=len(state.chunk_times),
        error=error,
    )


async def chat_once(
    *,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float = 0.0,
    api_key: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[bool, int, str, str]:
    """One non-streamed request; returns (ok, status, text, error)."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                base_url.rstrip("/") + CHAT_PATH,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
                },
            )
    except httpx.HTTPError as exc:
        return (False, 0, "", f"{type(exc).__name__}: {exc}")
    if response.status_code != httpx.codes.OK:
        return (False, response.status_code, "", response.text[:200])
    try:
        payload: Any = response.json()
    except json.JSONDecodeError as exc:
        return (False, response.status_code, "", f"invalid JSON: {exc}")
    if not isinstance(payload, dict):
        return (False, response.status_code, "", "response is not an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return (False, response.status_code, "", "response has no choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return (False, response.status_code, "", "response has no content")
    return (True, response.status_code, content, "")


async def tokenize(
    *,
    base_url: str,
    text: str,
    api_key: str | None = None,
    timeout_s: float = 30.0,
) -> int | None:
    """Prompt token count from llama-server's /tokenize, or None when absent."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                base_url.rstrip("/") + TOKENIZE_PATH,
                json={"content": text},
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError:
        return None
    if response.status_code != httpx.codes.OK:
        return None
    try:
        payload: Any = response.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    tokens = payload.get("tokens")
    return len(tokens) if isinstance(tokens, list) else None


async def server_models(base_url: str, api_key: str | None = None) -> list[str]:
    """Model ids the server advertises."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                base_url.rstrip("/") + MODELS_PATH,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"cannot reach {base_url}: {exc}") from exc
    if response.status_code != httpx.codes.OK:
        raise RuntimeError(f"{base_url}{MODELS_PATH} returned HTTP {response.status_code}")
    payload: Any = response.json()
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]
