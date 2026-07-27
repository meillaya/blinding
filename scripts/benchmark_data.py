"""Data acquisition for the benchmark report (todo 40): k6/locust parsers + probes.

Parses the newline-delimited k6 JSON streams (one JSON object PER LINE — never
``json.load`` the whole file) and the locust ``--csv`` stats, and probes
Prometheus / kubectl for the cluster-side numbers (replicas, model-load time,
cold start). Every missing input raises ``BenchError`` naming it — never NaN.
"""

from __future__ import annotations

import csv
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class BenchError(RuntimeError):
    """A named input (file, query, binary) is missing or unusable."""


@dataclass(frozen=True, slots=True)
class RunStats:
    """Load-generator aggregate for one scenario (latencies in ms)."""

    requests: int
    duration_s: float
    rps: float
    p95_ms: float
    p99_ms: float
    error_rate: float


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile (numpy 'linear' method) of a sorted list."""
    idx = pct / 100.0 * (len(sorted_vals) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def parse_k6(path: Path) -> RunStats:
    """Aggregate a k6 newline-delimited JSON stream (one object per line)."""
    if not path.is_file():
        raise BenchError(f"missing k6 JSON output: {path} (run scripts/run_load_test.sh first)")
    durations: list[float] = []
    failed = total = 0
    first = last = 0.0
    with path.open() as fh:
        for line in fh:
            # Substring prefilter: the streams are up to 1 GB+ — json.loads only
            # the three metrics we aggregate (cuts ~70% of parse work).
            if '"metric":"http_req_duration"' not in line or '"type":"Point"' not in line:
                if '"metric":"http_req_failed"' in line and '"type":"Point"' in line:
                    failed += int(json.loads(line)["data"]["value"] >= 0.5)
                    total += 1
                elif '"metric":"http_reqs"' in line and '"type":"Point"' in line:
                    ts = datetime.fromisoformat(json.loads(line)["data"]["time"]).timestamp()
                    first = ts if first == 0.0 else min(first, ts)
                    last = max(last, ts)
                continue
            durations.append(json.loads(line)["data"]["value"])
    if not durations:
        raise BenchError(f"{path}: no http_req_duration samples — truncated k6 run?")
    window = last - first
    if window <= 0:
        raise BenchError(f"{path}: single-timestamp stream, cannot derive RPS")
    durations.sort()
    return RunStats(
        requests=len(durations),
        duration_s=round(window, 2),
        rps=round(len(durations) / window, 1),
        p95_ms=round(_percentile(durations, 95.0), 1),
        p99_ms=round(_percentile(durations, 99.0), 1),
        error_rate=failed / total if total else 0.0,
    )


def parse_locust(path: Path) -> RunStats:
    """Aggregate the locust ``--csv`` stats file (its 'Aggregated' row)."""
    if not path.is_file():
        raise BenchError(f"missing locust CSV: {path} (run make load-test-locust first)")
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            if row["Name"].strip() == "Aggregated":
                count, failures = int(row["Request Count"]), int(row["Failure Count"])
                return RunStats(
                    requests=count,
                    duration_s=round(count / float(row["Requests/s"]), 1),
                    rps=round(float(row["Requests/s"]), 1),
                    p95_ms=float(row["95%"]),
                    p99_ms=float(row["99%"]),
                    error_rate=failures / count if count else 0.0,
                )
    raise BenchError(f"{path}: no 'Aggregated' row found")


def prom_query(base_url: str, expr: str, at: float | None = None) -> list[dict[str, Any]]:
    """Instant-query Prometheus; loud on unreachable server or bad expression."""
    params = {"query": expr, **({"time": f"{at:.3f}"} if at is not None else {})}
    url = f"{base_url}/api/v1/query?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 (localhost http)
            body = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BenchError(
            f"Prometheus unreachable at {base_url} ({exc}) — start the port-forward: "
            "kubectl -n monitoring port-forward svc/kube-prometheus-kube-prome-prometheus 9090:9090"
        ) from exc
    if body.get("status") != "success":
        raise BenchError(f"Prometheus query failed: {expr!r} -> {body.get('error', body)}")
    result: list[dict[str, Any]] = body["data"]["result"]
    return result


def prom_scalar(base_url: str, expr: str, at: float | None, what: str) -> float:
    """Exactly one numeric result, or a loud error naming the empty query."""
    result = prom_query(base_url, expr, at)
    if not result:
        raise BenchError(f"Prometheus returned no series for {what}: {expr!r}")
    return float(result[0]["value"][1])


def pod_cold_start_seconds(context: str, namespace: str) -> float:
    """Slowest model-server pod's creationTimestamp -> Ready transition (seconds).

    Real cold start = schedule + pull + boot + model load; the same image serves
    every CPU scenario, so one measurement represents all of them.
    """
    try:
        out = subprocess.run(
            ["kubectl", "--context", context, "-n", namespace, "get", "pods",
             "-l", "app.kubernetes.io/name=model-server", "-o", "json"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchError(f"kubectl cold-start probe failed ({exc}) — is the cluster up?") from exc
    worst = 0.0
    for pod in json.loads(out)["items"]:
        created = datetime.fromisoformat(pod["metadata"]["creationTimestamp"])
        ready = next(
            (c["lastTransitionTime"] for c in pod["status"].get("conditions", [])
             if c["type"] == "Ready" and c.get("lastTransitionTime")),
            None,
        )
        if ready is not None:
            worst = max(worst, (datetime.fromisoformat(ready) - created).total_seconds())
    if worst <= 0:
        raise BenchError("no Ready model-server pod found for the cold-start probe")
    return round(worst, 2)


def model_load_seconds(base_url: str, version: str, at: float | None) -> float:
    """Mean observed model-load time across pods for one model version."""
    expr = f'avg(inferenceops_model_load_seconds_sum{{model_version="{version}"}})'
    return round(prom_scalar(base_url, expr, at, f"model-load time v{version}"), 3)


def last_timestamp(path: Path) -> str:
    """The last http_reqs sample time in a k6 stream (anchors replica queries)."""
    last = ""
    with path.open() as fh:
        for line in fh:
            if '"metric":"http_reqs"' in line and '"type":"Point"' in line:
                last = json.loads(line)["data"]["time"]
    if not last:
        raise BenchError(f"{path}: no http_reqs samples to anchor the replica query")
    return last
