#!/usr/bin/env python3
"""Test double for llama-server.

It answers the handful of endpoints Blinding uses and accepts the flags the
real server receives, so the process supervisor and the client can be tested
without a model.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CHUNK_DELAY_S = 0.004
TOKENS_PER_CHUNK = 3


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
            content = str(body.get("content", ""))
            self._send_json(200, {"tokens": list(range(len(content.split())))})
            return
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return
        if os.environ.get("FAKE_SERVER_FAIL") == "1":
            self._send_json(500, {"error": "asked to fail"})
            return
        content = " ".join(["token"] * (TOKENS_PER_CHUNK * body.get("max_tokens", 1)))
        if body.get("stream"):
            self._stream(content, int(body.get("max_tokens", 1)))
            return
        self._send_json(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": TOKENS_PER_CHUNK},
            },
        )

    def _stream(self, content: str, max_tokens: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        words = content.split()
        for index in range(0, len(words), TOKENS_PER_CHUNK):
            piece = " ".join(words[index : index + TOKENS_PER_CHUNK]) + " "
            event = {
                "choices": [{"delta": {"content": piece}}],
            }
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
            time.sleep(CHUNK_DELAY_S)
        usage = {
            "choices": [],
            "usage": {"prompt_tokens": 4, "completion_tokens": max_tokens},
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
