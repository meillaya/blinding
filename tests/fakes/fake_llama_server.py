#!/usr/bin/env python3
"""Test double for llama-server.

It answers the handful of endpoints Blinding uses and accepts the flags the
real server receives, so the process supervisor and the client can be tested
without a model.

Environment variables tune the timing and the feature support each test needs.
Every value is read when a request arrives, so a test can set it before the
request without restarting the server:

    FAKE_TTFT_S              seconds before the first content chunk
    FAKE_CHUNK_DELAY_S       seconds after each content chunk (default 0.004)
    FAKE_TOKENS_PER_CHUNK    tokens carried by one chunk (default 3)
    FAKE_PROMPT_TOKENS       prompt count in usage blocks (default 4)
    FAKE_NO_TOKENIZE         1 => /tokenize answers 404
    FAKE_REJECT_STREAM_OPTIONS
                             1 => chat answers 400 when the body has the
                             stream_options key
    FAKE_SERVER_FAIL         1 => chat answers 500
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_TTFT_S = 0.0
DEFAULT_CHUNK_DELAY_S = 0.004
DEFAULT_TOKENS_PER_CHUNK = 3
DEFAULT_PROMPT_TOKENS = 4


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


def _env_flag(name: str) -> bool:
    return os.environ.get(name) == "1"


class Handler(BaseHTTPRequestHandler):
    """Minimal OpenAI surface with streamed chat completions."""

    server_version = "fake-llama"

    def log_message(self, *args: object) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/v1/models":
            self._send_json(200, {"data": [{"id": self.server.alias}]})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body: Any = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "bad json"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "bad body"})
            return
        if self.path == "/tokenize":
            if _env_flag("FAKE_NO_TOKENIZE"):
                self._send_json(404, {"error": "not found"})
                return
            content = str(body.get("content", ""))
            self._send_json(200, {"tokens": list(range(len(content.split())))})
            return
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return
        if _env_flag("FAKE_REJECT_STREAM_OPTIONS") and "stream_options" in body:
            self._send_json(400, {"error": "stream_options is not supported"})
            return
        if _env_flag("FAKE_SERVER_FAIL"):
            self._send_json(500, {"error": "asked to fail"})
            return
        max_tokens = int(body.get("max_tokens", 1))
        tokens_per_chunk = max(1, _env_int("FAKE_TOKENS_PER_CHUNK", DEFAULT_TOKENS_PER_CHUNK))
        prompt_tokens = _env_int("FAKE_PROMPT_TOKENS", DEFAULT_PROMPT_TOKENS)
        content = " ".join(["token"] * (tokens_per_chunk * max_tokens))
        if body.get("stream"):
            self._stream(content, max_tokens, tokens_per_chunk, prompt_tokens)
            return
        self._send_json(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": tokens_per_chunk},
            },
        )

    def _stream(
        self,
        content: str,
        max_tokens: int,
        tokens_per_chunk: int,
        prompt_tokens: int,
    ) -> None:
        ttft_s = _env_float("FAKE_TTFT_S", DEFAULT_TTFT_S)
        chunk_delay_s = _env_float("FAKE_CHUNK_DELAY_S", DEFAULT_CHUNK_DELAY_S)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        words = content.split()
        if words:
            time.sleep(ttft_s)
        for index in range(0, len(words), tokens_per_chunk):
            piece = " ".join(words[index : index + tokens_per_chunk]) + " "
            event = {
                "choices": [{"delta": {"content": piece}}],
            }
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
            time.sleep(chunk_delay_s)
        usage = {
            "choices": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": max_tokens},
        }
        self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", default="fake")
    parser.add_argument("--alias", default="fake-model")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--n-gpu-layers", type=int, default=0)
    args, _unknown = parser.parse_known_args()

    if args.n_gpu_layers > 0:
        print(
            f"load_tensors: offloaded {args.n_gpu_layers}/{args.n_gpu_layers} layers to GPU",
            flush=True,
        )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.alias = args.alias  # type: ignore[attr-defined]
    print(f"listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
