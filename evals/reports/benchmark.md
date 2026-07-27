# InferenceOps benchmark (real local numbers)

Generated 2026-07-27T06:11:16+00:00 from k6/locust runs in `evals/reports/` + live Prometheus. Cost model: one full node billed per replica (CPU $0.384/hr m6i.2xlarge, GPU $1.212/hr g5.2xlarge, us-east-1 on-demand); `cost/1k = replicas * node_$hr / (RPS * 3600) * 1000`.

| scenario | replicas | RPS | p95 (ms) | p99 (ms) | error rate | cost / 1k reqs | cold start (s) | model load (s) | GPU util | throughput / replica |
|---||---||---||---||---||---||---||---||---||---||---|
| baseline CPU (smoke, 2 VUs) | 2 | 37.1 | 54.4 | 54.8 | 0.00% | $0.0058 | 7 | 0.844 | documented (no GPU) | 18.6 |
| HPA enabled (spike, 70 VUs) | 6 | 2897.5 | 65.6 | 396.1 | 93.04% | $0.0002 | 7 | 0.844 | documented (no GPU) | 482.9 |
| soak (8 VUs, 90 s) | 5 | 137.9 | 60.6 | 62.4 | 0.00% | $0.0039 | 7 | 0.844 | documented (no GPU) | 27.6 |
| locust (50 users, think 0.5-1.5 s) | 2 | 45.4 | 59 | 72 | 0.00% | $0.0047 | 7 | 0.844 | documented (no GPU) | 22.7 |
| canary candidate (2.0.0, 10% split) | 1 | 2.7 | 73.8 | 74.8 | 0.00% | $0.0390 | 7 | 0.827 | documented (no GPU) | 2.7 |
| vLLM / GPU LLM (documented) | 1 (documented) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) |

Notes:
- replicas = peak `kube_deployment_status_replicas` over each run window, bracketed +90 s
  past the last sample because HPA scale-up lags the RPS metric ~30-50 s.
- cold start = slowest live model-server pod, creationTimestamp -> Ready (same image
  serves every CPU scenario); model load = `inferenceops_model_load_seconds`.
- canary row = candidate (`model_version="2.0.0"`) Prometheus metrics over the 90/10
  split; blended k6 numbers ride in `benchmark.json` (`raw.blended_k6_*`).
- GPU row: no GPU on this host (`DCGM_FI_DEV_GPU_UTIL` absent) — documented, not measured.
