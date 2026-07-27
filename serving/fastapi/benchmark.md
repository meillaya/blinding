# FastAPI model-server: serving benchmark

Performance summary for the FastAPI model-server (`apps/model-server`), the
implemented serving backend. The canonical, machine-generated table lives in
[`evals/reports/benchmark.md`](../../evals/reports/benchmark.md) (todo 40); this
page interprets the FastAPI rows and points at the raw runs. Regenerate the
source table with `uv run python scripts/gen_benchmark.py`.

## Headline numbers (real local runs)

All CPU rows below are the FastAPI model-server behind the gateway + nginx
ingress on the kind cluster (`make deploy-local`). Numbers are quoted from
[`evals/reports/benchmark.md`](../../evals/reports/benchmark.md); see that file
for the cost model and the replica-window methodology.

| scenario | replicas | RPS | p95 (ms) | error rate | cost / 1k reqs |
|---|---|---|---|---|---|
| baseline CPU (smoke, 2 VUs) | 2 | 37.1 | 54.4 | 0.00% | $0.0058 |
| HPA enabled (spike, 70 VUs) | 6 | 2897.5 | 65.6 | 93.04% | $0.0002 |
| soak (8 VUs, 90 s) | 5 | 137.9 | 60.6 | 0.00% | $0.0039 |
| locust (50 users, think 0.5-1.5 s) | 2 | 45.4 | 59 | 0.00% | $0.0047 |
| canary candidate (2.0.0, 10% split) | 1 | 2.7 | 73.8 | 0.00% | $0.0390 |

Startup: cold start 7 s (pod create to Ready, worst pod) and model load 0.844 s
(`inferenceops_model_load_seconds`), identical image across every CPU scenario.

## Reading the rows

- **Baseline / soak / locust are the steady-state picture.** p95 sits at 54-61 ms
  with 0% errors, roughly 8-10x headroom under the 500 ms latency SLO
  ([`docs/incident-runbook.md`](../../docs/incident-runbook.md)). The soak holds
  ~138 RPS clean; the HPA scales model-server 2 to 5 during it off the external
  RPS metric, then recovers to 2.
- **The spike row is load shedding by design, not a failure.** At 70 VUs the
  gateway sheds ~95% of the burst, so the 93.04% error rate is the intended
  spike signal (it trips the HPA, which scales 2 to 6). p95 stays low (65.6 ms)
  precisely because excess load is rejected early.
- **The canary row is a 10% traffic slice**, so its 2.7 RPS and $0.039/1k cost
  are artifacts of one replica billing a whole node against a tenth of traffic.
  It proves the candidate (model_version 2.0.0) serves cleanly under the split,
  not a throughput ceiling.
- **GPU / vLLM is documented, not measured.** This host has no GPU
  (`DCGM_FI_DEV_GPU_UTIL` absent), so the benchmark's GPU row reads
  `documented (no GPU)`. The GPU serving path is the vLLM backend in
  [`serving/vllm/`](../vllm/), not the FastAPI server.

## How these were produced

- Load generators: k6 (`scripts/run_load_test.sh`, scenarios smoke / stress /
  spike / soak, todo 37-38) and Locust (`load_tests/locust/locustfile.py`, todo 39).
- Metrics: live Prometheus (`inferenceops_request_count_total`,
  `inferenceops_model_load_seconds`) plus `kubectl` pod JSON for cold start.
- Generator: `scripts/gen_benchmark.py` (+ `scripts/benchmark_data.py`), which
  parses the k6 NDJSON streams in `evals/reports/` and writes both the markdown
  table and `evals/reports/benchmark.json`.

## See also

- [`evals/reports/benchmark.md`](../../evals/reports/benchmark.md): the canonical table + cost model + methodology notes.
- [`deployment.md`](./deployment.md): how this server is deployed to kind and EKS, plus the canary release flow.
- [`serving/README.md`](../README.md): FastAPI vs vLLM vs KServe vs Ray Serve tradeoffs.
- [`docs/autoscaling-strategy.md`](../../docs/autoscaling-strategy.md): the external-RPS HPA the spike/soak rows exercise.
