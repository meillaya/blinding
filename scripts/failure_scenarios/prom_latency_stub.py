"""Synthetic Prometheus query-API stub for the latency-regression scenario (todo 49).

Serves the two endpoints scripts/canary_check.sh talks to:

  * ``GET /-/ready``      -> 200 ``ok`` (the reachability probe), and
  * ``GET /api/v1/query`` -> a Prometheus vector response whose scalar is chosen
    by inspecting the PromQL ``query`` parameter.

It injects a deterministic p95 LATENCY REGRESSION: a ``histogram_quantile`` query
filtered to the CANDIDATE ``model_version`` returns a HIGH p95, the same query
filtered to the STABLE version returns a LOW p95, so canary_check.sh sees
``candidate p95 > stable p95 x 1.20`` and auto-rolls-back. Every other query
(error-rate ratio, DCGM GPU) returns an EMPTY vector (no errors / no GPU).

This is the ``PROM_BASE_URL`` stub technique from todo 30's QA: it makes the
rollback reproducible WITHOUT waiting for a real slow candidate to emit metrics
through a scrape window. Configuration is via environment:

  STUB_CAND_VER / STUB_STABLE_VER   the two model_version labels to discriminate
  STUB_CAND_P95 / STUB_STABLE_P95   the synthetic p95 scalars (default 0.9 / 0.1)
  STUB_PORT_FILE                    path the bound (ephemeral) port is written to

The server binds 127.0.0.1:0 (a free ephemeral port) and writes the chosen port
to STUB_PORT_FILE before serving, so the caller never races a fixed port.
"""

from __future__ import annotations

import http.server
import json
import os
import time
import urllib.parse

CAND = os.environ["STUB_CAND_VER"]
STABLE = os.environ["STUB_STABLE_VER"]
CAND_P95 = os.environ.get("STUB_CAND_P95", "0.9")
STABLE_P95 = os.environ.get("STUB_STABLE_P95", "0.1")
PORT_FILE = os.environ["STUB_PORT_FILE"]


def scalar(value: str) -> dict[str, object]:
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [time.time(), value]}]},
    }


def empty() -> dict[str, object]:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, obj: dict[str, object]) -> None:
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server requires this exact name
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/-/ready":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if parsed.path == "/api/v1/query":
            query = urllib.parse.parse_qs(parsed.query).get("query", [""])[0]
            if "histogram_quantile" in query:
                if f'model_version="{CAND}"' in query:
                    self._send_json(scalar(CAND_P95))
                elif f'model_version="{STABLE}"' in query:
                    self._send_json(scalar(STABLE_P95))
                else:
                    self._send_json(empty())
            else:
                self._send_json(empty())
            return
        self._send_json(empty())

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - match base signature (format is the base param name)
        pass


def main() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    with open(PORT_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(server.server_address[1]))
    server.serve_forever()


if __name__ == "__main__":
    main()
