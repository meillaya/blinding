"""Benchmark report generator — real numbers from k6/locust runs + Prometheus (todo 40).

Assembles ``evals/reports/benchmark.md`` (table) + ``benchmark.json`` from the
load-test artifacts (parsed by ``benchmark_data``), the documented cost model
below, and live Prometheus. Every missing input is a LOUD error — never a NaN.

Usage:
    kubectl -n monitoring port-forward svc/kube-prometheus-kube-prome-prometheus 9090:9090
    uv run python scripts/gen_benchmark.py [--prometheus-url http://localhost:9090]

Cost model (public on-demand list prices, us-east-1; one full node billed per
replica — conservative, since a replica only uses a fraction of a node):
    CPU_NODE_USD_HR = 0.384   # m6i.2xlarge (8 vCPU / 32 GiB)
    GPU_NODE_USD_HR = 1.212   # g5.2xlarge (NVIDIA A10G, 8 vCPU / 32 GiB)
    cost_per_1k = replicas * node_usd_hr / (rps * 3600) * 1000
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark_data import (
    BenchError,
    RunStats,
    last_timestamp,
    model_load_seconds,
    parse_k6,
    parse_locust,
    pod_cold_start_seconds,
    prom_query,
    prom_scalar,
)

CPU_NODE_USD_HR = 0.384  # m6i.2xlarge on-demand, us-east-1
GPU_NODE_USD_HR = 1.212  # g5.2xlarge (A10G) on-demand, us-east-1 (GPU row is documented-only)
GPU_UTIL_METRIC = "DCGM_FI_DEV_GPU_UTIL"
NO_GPU = "documented (no GPU)"
COLUMNS = (
    "scenario", "replicas", "RPS", "p95 (ms)", "p99 (ms)", "error rate",
    "cost / 1k reqs", "cold start (s)", "model load (s)", "GPU util", "throughput / replica",
)


@dataclass(frozen=True, slots=True)
class Row:
    """One benchmark table row; every cell renders to a non-empty string."""

    scenario: str
    replicas: str
    rps: str
    p95: str
    p99: str
    error_rate: str
    cost_per_1k: str
    cold_start: str
    model_load: str
    gpu_util: str
    per_replica: str
    raw: dict[str, Any]

    def cells(self) -> tuple[str, ...]:
        return (
            self.scenario, self.replicas, self.rps, self.p95, self.p99, self.error_rate,
            self.cost_per_1k, self.cold_start, self.model_load, self.gpu_util, self.per_replica,
        )


@dataclass(frozen=True, slots=True)
class ClusterFacts:
    """Cluster-side measurements one row renders (replicas are per-scenario peak)."""

    replicas: float
    cold_start: float
    model_load: float
    gpu_util: str


def cost_per_1k(replicas: float, rps: float, node_usd_hr: float = CPU_NODE_USD_HR) -> str:
    return f"${replicas * node_usd_hr / (rps * 3600) * 1000:.4f}"


def run_row(name: str, source: str, stats: RunStats, facts: ClusterFacts) -> Row:
    return Row(
        scenario=name, replicas=f"{facts.replicas:g}", rps=f"{stats.rps:g}",
        p95=f"{stats.p95_ms:g}", p99=f"{stats.p99_ms:g}",
        error_rate=f"{stats.error_rate:.2%}",
        cost_per_1k=cost_per_1k(facts.replicas, stats.rps),
        cold_start=f"{facts.cold_start:g}", model_load=f"{facts.model_load:g}",
        gpu_util=facts.gpu_util, per_replica=f"{stats.rps / facts.replicas:.1f}",
        raw={"source": source, "requests": stats.requests, "window_s": stats.duration_s,
             "error_rate": round(stats.error_rate, 6), "replicas": facts.replicas},
    )


def canary_row(reports: Path, prom: str, cold_start: float, gpu_util: str) -> Row:  # noqa: E501
    """Candidate-vs-stable row from Prometheus, discriminated by model_version."""
    path = reports / "k6-canary.json"
    blended = parse_k6(path)  # loud if the canary run never happened
    # +20 s: the final scrape lands ~15 s past the last sample; the 80 s rate
    # window then brackets the run itself, so rate() is barely diluted by idle.
    at = datetime.fromisoformat(last_timestamp(path)).timestamp() + 20
    version = "2.0.0"
    win = "80s"
    rate = f'sum(rate(inferenceops_request_count_total{{model_version="{version}"}}[{win}]))'
    bucket = f'inferenceops_request_duration_seconds_bucket{{model_version="{version}"}}'
    quantile = f'histogram_quantile(0.9{{p}}, sum by (le) (rate({bucket}[{win}])))'
    err = f'sum(rate(inferenceops_error_count_total{{model_version="{version}"}}[{win}])) / {rate}'
    rps = prom_scalar(prom, rate, at, "canary candidate RPS")
    errors = prom_query(prom, err, at)
    canary_replicas = prom_scalar(
        prom, 'kube_deployment_status_replicas{deployment="model-server-canary"}',
        at, "canary replicas",
    )
    return Row(
        scenario=f"canary candidate ({version}, 10% split)",
        replicas=f"{canary_replicas:g}",
        rps=f"{rps:.1f}",
        p95=f"{prom_scalar(prom, quantile.replace('{p}', '5'), at, 'canary p95') * 1000:.1f}",
        p99=f"{prom_scalar(prom, quantile.replace('{p}', '9'), at, 'canary p99') * 1000:.1f}",
        error_rate=f"{(float(errors[0]['value'][1]) if errors else 0.0):.2%}",
        cost_per_1k=cost_per_1k(1.0, rps),
        cold_start=f"{cold_start:g}",
        model_load=f"{model_load_seconds(prom, version, at):g}",
        gpu_util=gpu_util, per_replica=f"{rps:.1f}",
        raw={"source": str(path), "candidate": version, "split": "90/10",
             "blended_k6_rps": blended.rps, "blended_k6_p95_ms": blended.p95_ms,
             "blended_k6_error_rate": round(blended.error_rate, 6), "anchor_unix": round(at)},
    )


def gpu_row() -> Row:
    return Row(
        scenario="vLLM / GPU LLM (documented)", replicas="1 (documented)",
        rps=NO_GPU, p95=NO_GPU, p99=NO_GPU, error_rate=NO_GPU, cost_per_1k=NO_GPU,
        cold_start=NO_GPU, model_load=NO_GPU, gpu_util=NO_GPU, per_replica=NO_GPU,
        raw={"source": "documented", "note": f"no GPU on host; {GPU_UTIL_METRIC} absent",
             "gpu_node_usd_hr": GPU_NODE_USD_HR},
    )


def _render_markdown(rows: list[Row]) -> str:
    lines = [
        "# InferenceOps benchmark (real local numbers)",
        "",
        f"Generated {datetime.now(tz=UTC).isoformat(timespec='seconds')} from k6/locust runs in "
        f"`evals/reports/` + live Prometheus. Cost model: one full node billed per replica (CPU "
        f"${CPU_NODE_USD_HR}/hr m6i.2xlarge, GPU ${GPU_NODE_USD_HR}/hr g5.2xlarge, us-east-1 "
        "on-demand); `cost/1k = replicas * node_$hr / (RPS * 3600) * 1000`.",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "|".join("---|" for _ in COLUMNS),
        *(("| " + " | ".join(r.cells()) + " |") for r in rows),
        "",
        "Notes:",
        "- replicas = peak `kube_deployment_status_replicas` over each run window, bracketed +90 s",
        "  past the last sample because HPA scale-up lags the RPS metric ~30-50 s.",
        "- cold start = slowest live model-server pod, creationTimestamp -> Ready (same image",
        "  serves every CPU scenario); model load = `inferenceops_model_load_seconds`.",
        "- canary row = candidate (`model_version=\"2.0.0\"`) Prometheus metrics over the 90/10",
        "  split; blended k6 numbers ride in `benchmark.json` (`raw.blended_k6_*`).",
        f"- GPU row: no GPU on this host (`{GPU_UTIL_METRIC}` absent) — documented, not measured.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark report generator (todo 40).")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--reports-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--context", default="kind-inferenceops")
    parser.add_argument("--namespace", default="inferenceops")
    args = parser.parse_args()
    reports: Path = args.reports_dir
    prom: str = args.prometheus_url

    gpu_series = prom_query(prom, GPU_UTIL_METRIC)
    gpu_util = f"{float(gpu_series[0]['value'][1]):g}%" if gpu_series else NO_GPU
    cold_start = pod_cold_start_seconds(args.context, args.namespace)
    load_stable = model_load_seconds(prom, "1.0.0", None)

    rows: list[Row] = []
    for name, fname in (
        ("baseline CPU (smoke, 2 VUs)", "k6-smoke.json"),
        ("HPA enabled (spike, 70 VUs)", "k6-spike.json"),
        ("soak (8 VUs, 90 s)", "k6-soak.json"),
    ):
        path = reports / fname
        stats = parse_k6(path)
        # Replica window = (first sample - 30 s, last sample + 90 s]: brackets the
        # run plus the HPA's late scale-up (metric lag, learnings todo 38) WITHOUT
        # reaching into the previous scenario's peak (runs sit ~2 min apart).
        last_ts = datetime.fromisoformat(last_timestamp(path)).timestamp()
        anchor = last_ts + 90
        window = stats.duration_s + 120
        replicas = prom_scalar(
            prom,
            'max_over_time(kube_deployment_status_replicas'
            f'{{deployment="model-server",namespace="{args.namespace}"}}'
            f'[{window:.0f}s] @ {anchor:.0f})',
            anchor, f"peak replicas during {fname}",
        )
        rows.append(run_row(
            name, str(path), stats,
            ClusterFacts(replicas, cold_start, load_stable, gpu_util),
        ))

    locust_path = reports / "locust_stats.csv"
    if locust_path.is_file():
        locust_at = locust_path.stat().st_mtime
        locust_replicas = prom_scalar(
            prom,
            'max_over_time(kube_deployment_status_replicas'
            f'{{deployment="model-server",namespace="{args.namespace}"}}[2m] @ {locust_at:.0f})',
            None, "replicas during locust run",
        )
        rows.append(run_row(
            "locust (50 users, think 0.5-1.5 s)", str(locust_path), parse_locust(locust_path),
            ClusterFacts(locust_replicas, cold_start, load_stable, gpu_util),
        ))

    rows.append(canary_row(reports, prom, cold_start, gpu_util))
    rows.append(gpu_row())

    real_rows = sum(1 for r in rows if r.raw.get("source") != "documented")
    if real_rows < 3:
        raise BenchError(f"only {real_rows} real scenario rows — need >= 3")

    report = _render_markdown(rows)
    md_path, json_path = reports / "benchmark.md", reports / "benchmark.json"
    md_path.write_text(report)
    json_path.write_text(json.dumps(
        {"generated_at": datetime.now(tz=UTC).isoformat(), "prometheus": prom,
         "cost_model": {"cpu_node_usd_hr": CPU_NODE_USD_HR, "gpu_node_usd_hr": GPU_NODE_USD_HR,
                        "formula": "replicas * node_usd_hr / (rps * 3600) * 1000"},
         "columns": list(COLUMNS),
         "rows": [{**dict(zip(COLUMNS, r.cells(), strict=True)), "raw": r.raw} for r in rows]},
        indent=2) + "\n")
    print(report)
    print(f"\nwrote {md_path} + {json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchError as exc:
        print(f"gen_benchmark: ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
