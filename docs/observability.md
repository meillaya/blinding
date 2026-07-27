# Observability

The serving platform is observable through three signals (metrics, traces, logs)
and one managed fallback on EKS. This doc covers what each component owns, the
SLI/SLO contract, the dashboards and alerts, and a worked latency-incident
walkthrough. The response procedures for every alert live in
[incident-runbook.md](incident-runbook.md); this page is the map, the runbook
is the procedure.

## The stack and what each piece owns

| Component | Role | Deployed by | Config in this repo |
|---|---|---|---|
| **Prometheus** | Metrics scrape, storage, alert evaluation | kube-prometheus-stack (Helm) | `observability/prometheus/values.yaml`, rules in `observability/prometheus/rules/*.yaml` |
| **Grafana** | The four dashboards | kube-prometheus-stack Grafana + dashboard sidecar | `observability/grafana/dashboards/*.json` |
| **OpenTelemetry collector** | Distributed traces: OTLP receive → batch → export | `k8s/base/otel-collector.yaml` (in the `inferenceops` namespace) | `observability/opentelemetry/collector.yaml` |
| **Loki** | Log aggregation | loki-stack Helm chart + promtail DaemonSet | `observability/loki/values.yaml` |
| **CloudWatch** | EKS control-plane logs, optional Container Insights | Terraform `eks` module (**EKS only**) | `infra/terraform/modules/eks` |

Signal flow:

```
metrics:  model-server /metrics ─(ServiceMonitor, 15s scrape)─> Prometheus ─> alerts + Grafana
          (inferenceops_* series; DCGM_FI_DEV_* where GPUs exist)

traces:   gateway + model-server ─(OTLP/HTTP)─> otel-collector ─(batch)─> exporter
          (init_telemetry + ASGI middleware; log-and-drop if the collector is down)

logs:     container stdout ─(promtail DaemonSet, tails /var/log/containers)─> Loki
          (e.g. {namespace="inferenceops", pod=~"model-server-.*"})
```

Notes on the pieces:

- **Prometheus** selects the model-server ServiceMonitors by the
  `release: kube-prometheus` label (`serviceMonitorSelectorNilUsesHelmValues`
  made explicit in values.yaml). The alert rules ride in
  `additionalPrometheusRulesMap`; the standalone `rules/*.yaml` files are the
  authoritative reference (full PrometheusRule CR shape for a direct
  `kubectl apply` outside Helm).
- **Grafana** loads dashboards through the sidecar: a ConfigMap labeled
  `grafana_dashboard: "1"` (built by `kubectl apply -k
  observability/grafana/dashboards`) is hot-loaded within a few seconds and
  survives Grafana pod rollouts, because the ConfigMap persists even when the
  release churns. Every panel binds its datasource to uid `prometheus`.
- **The OTel collector** is traces-only for the apps. The model-server's
  metrics story stays Prometheus text exposition (`metrics.py`); the collector
  re-exposes OTLP metrics on `:8889` and self-metrics on `:8888`
  (`otelcol_receiver_accepted_spans` is the "are spans arriving" counter).
- **Loki** needs zero custom scrape config: promtail's default tail plus
  kubernetes_sd relabeling makes `{namespace="inferenceops"}` queries work out
  of the box. uvicorn access lines (`"POST /v1/predict HTTP/1.1" 200 OK`) are
  the per-request evidence.
- **CloudWatch** is the EKS-only tier. On EKS the cluster emits control-plane
  logs (api, audit, authenticator, controllerManager, scheduler) to CloudWatch
  Logs through the Terraform `eks` module; Container Insights can ship node and
  pod metrics there too. On kind there is no CloudWatch, and the in-cluster
  Prometheus/Grafana/Loki stack is the whole story.

## SLI / SLO

Four SLIs, each computed from metrics the model server already exports
(`inferenceops_*`) plus the DCGM exporter where GPUs are present. Each SLO is
measured with the **same expression as its alert**, so the SLO and the alert can
never disagree about the number.

| SLI | SLO target | Window | Alert |
|---|---|---|---|
| **Latency** (p95, end-to-end) | p95 < 500 ms in 99.5% of 5-minute windows | 30 days rolling | `HighP95Latency` |
| **Error rate** | < 1% in 99.5% of 5-minute windows | 30 days rolling | `HighErrorRate` |
| **Throughput** | serve ≥ 50 req/s while the latency and error SLOs hold | sustained capacity floor | none (tested offline by k6) |
| **GPU utilization** | < 90% sustained where GPUs are deployed | 5-minute windows | `HighGpuUtilization`, `HighGpuMemoryUsage` (both `info`) |

The PromQL behind them:

```promql
# latency p95 (per-version views append: by (le, model_version))
histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket[5m])) by (le))

# error rate
sum(rate(inferenceops_error_count_total[5m])) / sum(rate(inferenceops_request_count_total[5m]))

# throughput
sum(rate(inferenceops_request_count_total[5m]))

# GPU utilization (series absent on CPU-only nodes)
avg(DCGM_FI_DEV_GPU_UTIL)
```

**Error budget.** 99.5% over 30 days leaves 0.5%, about 216 minutes of breach
(roughly 7 minutes per day). When the 30-day burn is exhausted, freeze risky
deploys and model promotions until the window recovers.

**Current alerting shape.** The rules are single-window (5m rate, `for: 2m`).
They catch sustained breaches, not 4-minute fast-burn spikes. Multi-window,
multi-burn-rate recording rules are tracked as follow-up work; until then, a
short spike that stays under 2 minutes will not page but still counts against
the budget on the dashboards.

## The four dashboards

All four live in `observability/grafana/dashboards/` (one JSON source each,
wrapped into a sidecar ConfigMap by the kustomization in the same directory):

| Dashboard | Panels | Answers |
|---|---|---|
| **InferenceOps / Inference Overview** | Request rate (RPS); Latency percentiles (p50/p95/p99); Error rate; Replicas (model-server) | Is the service up, fast, and correctly sized? The first stop for any alert. |
| **InferenceOps / Latency Breakdown** | Stage p95 (queue / model-load / inference); Model load time (p50/p95); Inference latency (p50/p95/p99); Average batch size (rows/request) | *Where* the latency went: queueing, artifact reload, or the model call itself. |
| **InferenceOps / GPU Utilization** | GPU core utilization; GPU memory copy utilization; Framebuffer memory (used/free); Token throughput (tokens/sec) | GPU headroom and LLM token rate. Empty on the CPU-only kind cluster (no DCGM series), by design. |
| **InferenceOps / Model Quality** | Prediction distribution (rate by bucket); Confidence quantiles (p50/p95); Drift score; Eval accuracy (golden dataset); Model version in production | Is the model still *right*? Drift and quality, not just speed. |

Panels can be valid-but-empty by design: the expressions are correct, and the
series appear when the exporter or metric lands (DCGM on a GPU node,
`inferenceops_drift_score` once the drift worker runs, `inferenceops_tokens_total`
with an LLM backend).

## The alert rules

Three rule files in `observability/prometheus/rules/`, four alerts. Coverage is
enforced: `scripts/check_runbook_coverage.sh` fails if any alert name lacks a
`### <AlertName>` entry in [incident-runbook.md](incident-runbook.md).

| Alert | Severity | Expression | `for` | Rule file |
|---|---|---|---|---|
| `HighP95Latency` | warning | `histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket[5m])) by (le)) > 0.5` | 2m | `latency-alerts.yaml` |
| `HighErrorRate` | critical | `sum(rate(inferenceops_error_count_total[5m])) / sum(rate(inferenceops_request_count_total[5m])) > 0.01` | 2m | `error-rate-alerts.yaml` |
| `HighGpuUtilization` | info | `DCGM_FI_DEV_GPU_UTIL > 90` | 5m | `gpu-alerts.yaml` |
| `HighGpuMemoryUsage` | info | `(DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)) > 0.9` | 5m | `gpu-alerts.yaml` |

The two GPU alerts are valid PromQL with no GPU present: the DCGM series are
simply absent, the expressions evaluate to no result, and the alerts stay
inactive forever on the kind cluster. `HighErrorRate` is similarly self-guarded
under zero traffic: `rate()` of a counter with no samples yields no series, so
the division produces no result and the alert stays silent.

## Debugging a latency incident (walkthrough)

`HighP95Latency` fired. Here is the path from page to fix; the full procedure
with the error and GPU alerts is in
[incident-runbook.md](incident-runbook.md).

1. **Open `InferenceOps / Latency Breakdown`.** Find the dominant stage: queue
   wait, model load, or inference. The stage tells you the rest of the hunt.
2. **Split by version** to localize the regression:
   ```promql
   histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket[5m])) by (le, model_version))
   ```
   If one `model_version` owns the tail, a deploy or canary is the likely cause.
   The blended p95 is unforgiving: a slow canary serving just 10% of traffic can
   push the global p95 into the slow tail and trip the alert on its own.
3. **Check the canary.** If one is in flight, `scripts/canary_check.sh` (30s
   loop) already compares candidate vs stable per-version p95 and auto-rolls
   back when candidate > stable × 1.20. To act by hand: `make rollback`, then
   verify the base ingress is restored with several consecutive correct-version
   responses (the nginx data plane reconciles across replicas over a few
   seconds, so one probe proves nothing).
4. **Check capacity.** `kubectl -n inferenceops get hpa model-server`. Replicas
   pinned at the max with high CPU is a capacity problem: raise the HPA ceiling
   or shed load. If the external RPS metric shows `<unknown>`, prometheus-adapter
   is down and the HPA is holding (it fails safe, never scales on partial data);
   the cpu/memory metrics still drive it. See
   [autoscaling-strategy.md](autoscaling-strategy.md) for the metric flow and
   its ~30–50s lag.
5. **Check for a model-reload storm.** Spikes in `inferenceops_model_load_seconds`
   (the Latency Breakdown "Model load time" panel) mean pods are restarting and
   re-loading the artifact: crash-loop or OOM. Inspect `kubectl -n inferenceops
   get events`, then the pod logs in Loki:
   `{namespace="inferenceops", pod=~"model-server-.*"}`.
6. **Roll back if it is the canary; escalate if it is not.** If the error SLO is
   burning too, treat it as `HighErrorRate` (critical): roll back the most
   recent change now, debug after. If latency persists 10 minutes past rolling
   back the latest change, the problem is infrastructure (gateway, cluster,
   dependencies), not the model.

After any user-facing impact, open a blameless postmortem with the template in
the runbook. The worked example there (canary 2.0.0, ~6 minutes of breach,
auto-rolled back before the page landed) shows the expected level of detail.
