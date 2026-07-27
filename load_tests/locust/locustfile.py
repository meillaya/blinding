"""Locust load test for POST /v1/predict (todo 39, Wave 8).

The alternative interactive load tool to k6 (todo 37/38): drives the SAME
predict request through the live ingress and validates responses semantically
(status 200 AND a `prediction` field) so failures are counted correctly, not
just at the HTTP level.

Driven headless by `make load-test-locust` (writes --csv/--html reports under
evals/reports/). Standalone:
    uv run --with locust locust -f load_tests/locust/locustfile.py --headless \
        -u 50 -r 10 -t 30s --host http://localhost:8080
"""

from __future__ import annotations

import os
from typing import Final

from locust import HttpUser, between, task

# Routing + auth for the nginx ingress (same contract as load_tests/k6/smoke.js):
# the Host header selects the inferenceops virtual host, the API key
# authenticates the base path (/healthz is open). Env-overridable for parity
# with the k6 scripts' HOST_HEADER / API_KEY knobs.
_HOST_HEADER: Final[str] = os.environ.get("HOST_HEADER", "inferenceops.local")
_API_KEY: Final[str] = os.environ.get("API_KEY", "dev-key-REPLACE-ME-0000000000000000")

# 20 features — matches the fraud-detector model's expected input width.
_PAYLOAD: Final[dict[str, list[float]]] = {
    "features": [
        0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
        0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
    ],
}


class PredictUser(HttpUser):
    """A simulated client driving POST /v1/predict through the gateway."""

    # The CLI --host (Makefile: http://localhost:8080) overrides this default.
    host = "http://localhost:8080"

    # Think time keeps 50 users at a MODERATE ~35-50 RPS (cf. k6 soak: 8 VUs ≈
    # 150 RPS at 0% errors). Back-to-back would attempt ~900 RPS and trip the
    # gateway's load shedding (k6 stress: 94% errors at 80 VUs). The cluster
    # recently recovered from host swap-thrash (see learnings.md) — stay bounded.
    wait_time = between(0.5, 1.5)

    def on_start(self) -> None:
        self.client.headers = {
            "Content-Type": "application/json",
            "Host": _HOST_HEADER,
            "X-API-Key": _API_KEY,
        }

    @task
    def predict(self) -> None:
        with self.client.post("/v1/predict", json=_PAYLOAD, catch_response=True) as response:
            if response.status_code != 200:
                # Connection error (locust sets status 0) or non-200: leave the
                # outcome to locust's default raise_for_status path, which records
                # the REAL exception (ConnectionRefused, HTTPError, ...) as the
                # failure — calling .json() here would only mask it.
                return
            try:
                body = response.json()
            except ValueError:  # requests' JSONDecodeError subclasses ValueError
                response.failure("non-JSON response body")
                return
            if "prediction" not in body:
                response.failure("response missing 'prediction'")
                return
            response.success()
