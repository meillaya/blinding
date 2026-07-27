# Architecture

InferenceOps is an end-to-end MLOps platform for training, serving, and
evaluating ML models. The reference workload is a fraud-detection classifier
(scikit-learn, 20 features) served over HTTP by a FastAPI model server, with
the model lifecycle (train → evaluate → register → canary → promote) scripted
end to end. Two deployment targets exist: a local kind cluster for development
and EKS on AWS for production.

This doc explains the topology, what each component owns, and the two flows
that tie the system together: the request path and the model lifecycle. The
AWS-specific topology is summarized here and walked through in full in
[aws-deployment.md](aws-deployment.md).

---

## 1. Serving topology (the request path)

```
                          ┌───────────────────────────────────────────────┐
                          │                 ingress-nginx                 │
  client ────────────────▶│  Ingress "inferenceops"                       │
  (curl, k6, locust)      │  host inferenceops.local                      │
                          │  path / ──▶ service-gateway:8080              │
                          └───────────────────┬───────────────────────────┘
                                              ▼
                          ┌───────────────────────────────────────────────┐
                          │  gateway  (Deployment, ≥2 replicas)           │
                          │  • API-key auth   (X-API-Key)                 │
                          │  • rate limiting  (RATE_LIMIT_RPS)            │
                          │  • OTel spans + request counters              │
                          └───────────────────┬───────────────────────────┘
                                              │ http://service-model-server:8080
                                              ▼
                          ┌───────────────────────────────────────────────┐
                          │  model-server  (Deployment, ≥2, HPA 2..10)    │
                          │  • /v1/predict, /v1/batch, /v1/models         │
                          │  • DynamicBatcher ──▶ predictor (scikit-learn)│
                          │  • /metrics, /healthz, /readyz                │
                          └───────────────────┬───────────────────────────┘
                                              │ model loaded at startup
                                              ▼
                          ┌───────────────────────────────────────────────┐
                          │  model artifact via the ModelStorage ABC      │
                          │  • LocalStorageBackend (models/artifacts/)    │
                          │  • S3StorageBackend (s3-module bucket, IRSA)  │
                          └───────────────────────────────────────────────┘
```

### Component responsibilities

| Component | Manifest + code | Owns |
|---|---|---|
| ingress-nginx | [k8s/base/ingress.yaml](../k8s/base/ingress.yaml) | The L7 public edge. nginx `ingressClassName`, host `inferenceops.local`, `path / → service-gateway:8080`. On kind the controller is published through `extraPortMappings` (`8080:80`, `8443:443`) in [infra/scripts/kind-config.yaml](../infra/scripts/kind-config.yaml), so the edge is `localhost:8080`. |
| gateway | [k8s/base/deployment-gateway.yaml](../k8s/base/deployment-gateway.yaml), `apps/gateway/src/inferenceops_gateway/` | The **only** public entrypoint. `auth.py` validates `X-API-Key` against the `gateway-api-keys` Secret; `rate_limit.py` enforces `RATE_LIMIT_RPS`; `telemetry.py` emits OTel spans; then it proxies to `http://service-model-server:8080`. |
| model-server | [k8s/base/deployment-model-server.yaml](../k8s/base/deployment-model-server.yaml), `apps/model-server/src/inferenceops_model_server/` | Serves `/v1/predict`, `/v1/batch`, `/v1/models` on port 8080. `batching.py` (a `DynamicBatcher`) coalesces concurrent single-row predictions into one model call; `predictor.py` runs the scikit-learn predict; `metrics.py` exposes the `inferenceops_*` Prometheus series; `/readyz` answers 503 until the model finishes loading. |
| model artifact | `apps/model-server/src/inferenceops_model_server/storage/` | The `ModelStorage` abstraction with two backends: `LocalStorageBackend` (reads `models/artifacts/`) and `S3StorageBackend` (reads the bucket the [s3 module](../infra/terraform/modules/s3/) creates, via IRSA). `model_loader.py` resolves the `(MODEL_ID, MODEL_VERSION)` pair from the `inferenceops-config` ConfigMap and caches loaded models by identity. |
| worker | `apps/worker/src/inferenceops_worker/` | Offline compute: `batch_inference.py` (JSONL/CSV batch scoring), `drift_job.py` (PSI drift score), `report_generator.py`. Shares the model-server's storage + loader; runs as a CLI, not a long-lived service. |

### Request path, step by step

1. The client sends `POST /v1/predict` to the nginx edge with `Host: inferenceops.local` and an `X-API-Key` header.
2. The Ingress ([k8s/base/ingress.yaml](../k8s/base/ingress.yaml)) forwards to `service-gateway:8080`. The model-server Service is deliberately **not** exposed at the edge.
3. The gateway authenticates the key, applies the rate limit, opens a trace span, and proxies the request in-cluster.
4. The model-server's `DynamicBatcher` merges the request with any other in-flight predictions and calls the model once for the whole batch. The response carries `model_version`, which is what makes canary splits observable per request.
5. Every hop records metrics (`inferenceops_request_count_total`, latency histograms) on `/metrics` and exports spans to the OTel collector.

The Kubernetes reasoning behind the manifest fields (probes, rollout strategy,
graceful shutdown, anti-affinity) lives in
[kubernetes-ml-workloads.md](kubernetes-ml-workloads.md).

---

## 2. Model lifecycle

```
  train ──▶ evaluate ──▶ register ──▶ approve ──▶ upload ──▶ canary ──▶ promote
   │          │             │           │           │          │          │
   │       accuracy      registry     eval wiring  S3 or     nginx      weight
   │       ≥ 0.85 +      record +     flips        local     10% split  10 → 100,
   │       regression    model card   approved_    artifact  + health   manual
   │       gate                       for_prod     publish   checks     gate
   └─────────────────────── scripts/ + models/ ─────────────────┴──▶ rollback
                                                                    (automated on
                                                                     breach, or manual)
```

| Stage | Driver | Gate / effect |
|---|---|---|
| Train | `models/training/train.py` (config: `models/training/config.yaml`) | Seeded, deterministic artifact at `models/artifacts/fraud_detector/` |
| Evaluate | `models/training/evaluate.py` via `scripts/run_evals.sh` | Accuracy ≥ 0.85 **and** a regression gate vs the baseline (a drop > 3% on f1 fails the run) |
| Register | `models/registry/register_model.py` → `models/registry/registry_store.py` | Writes the registry record + a model card from `models/registry/model_card_template.md` |
| Approve | `models/registry/approve_model.py` | The **only** writer of `approved_for_prod` is the eval wiring; promote reads it |
| Upload | `scripts/upload_model.sh` | Writes `model.pkl` first and `model_meta.json` last, so a partial upload stays invisible (the metadata publishes the artifact) |
| Canary | `scripts/deploy_canary.sh` + [k8s/canary/](../k8s/canary/) | Deploys the candidate tier at `MODEL_VERSION=2.0.0` behind a 10% nginx canary-weight split |
| Check | `scripts/canary_check.sh` | Compares candidate-vs-stable p95 / error rate by the `model_version` label; auto-rolls back on breach (e.g. candidate p95 > stable × 1.20) |
| Promote | `scripts/promote.sh` | Raises canary-weight to 100. A deliberate manual gate, never auto-promoted |
| Rollback | `scripts/rollback.sh` | Deletes the canary tier and restores the base ingress (auth back in path). Idempotent from any canary state |

CI wiring in [.github/workflows/](../.github/workflows/): `ci.yml` (lint +
test on every push), `model-eval-gate.yml` (runs `make eval` on every PR from a
fresh clone), `model-upload.yml` (tag-triggered on `model-*`: trains, then
uploads with OIDC AWS auth), `build-and-push.yml` (builds the app images and
pushes them to ECR).

The full canary walkthrough (request topology, candidate artifact baking,
observing the split) is in [serving/fastapi/deployment.md](../serving/fastapi/deployment.md) §4.

---

## 3. Canary topology

During a canary window the base ingress is **replaced** for host
`inferenceops.local`, and the split happens at the model-server tier:

```
                              ┌── 90% ──▶ service-model-server         (stable,    model_version=1.0.0)
  client ──▶ nginx canary pair┤
                              └── 10% ──▶ service-model-server-canary  (candidate, model_version=2.0.0)

  k8s/canary/stable-ingress.yaml   Ingress "inferenceops"          ──▶ service-model-server
  k8s/canary/canary-ingress.yaml   Ingress "inferenceops-canary"   ──▶ service-model-server-canary
                                 nginx.ingress.kubernetes.io/canary: "true"
                                 nginx.ingress.kubernetes.io/canary-weight: "10"
```

Both Ingresses share the host and `ingressClassName: nginx`, which the
controller needs to merge the canary into the main server block and split by
weight. The gateway (auth + rate-limit) is bypassed during the window so the
90/10 split lands cleanly on the model tier; `make rollback` reinstates the
base ingress ([k8s/base/ingress.yaml](../k8s/base/ingress.yaml), `→ service-gateway`) and restores auth.
The candidate tier itself is [k8s/canary/canary-deployment.yaml](../k8s/canary/canary-deployment.yaml):
one replica (the split is by weight, not replica count), same image as stable,
`MODEL_VERSION=2.0.0` stamped on every response and every metric.

---

## 4. Observability

```
  model-server /metrics ──┐                    ┌──▶ Grafana dashboards
  gateway     /metrics ───┤ (ServiceMonitor)   │    (observability/grafana/dashboards/)
                          ▼                    │
                     Prometheus ───────────────┤
                     (kube-prometheus-stack)   ├──▶ prometheus-adapter ──▶ external.metrics.k8s.io ──▶ HPA
                          │                    │    (inferenceops_rps)
                          ▼                    │
                     alert rules ──────────────┘
                     (HighP95Latency, HighErrorRate, HighGpu*)

  gateway + model-server ──OTLP/HTTP──▶ otel-collector ──▶ trace backend (Tempo/Jaeger)

  pod stdout ──promtail──▶ Loki ──▶ Grafana
```

| Signal | Pipeline | Config |
|---|---|---|
| Metrics | model-server + gateway expose `/metrics`; Prometheus scrapes them through a ServiceMonitor (15s interval) | [observability/prometheus/values.yaml](../observability/prometheus/values.yaml), [k8s/overlays/local/servicemonitor-model-server.yaml](../k8s/overlays/local/servicemonitor-model-server.yaml) |
| Autoscaling feed | Prometheus → prometheus-adapter → the external metric `inferenceops_rps` → the HPA | [observability/prometheus-adapter/values.yaml](../observability/prometheus-adapter/values.yaml), [k8s/base/hpa.yaml](../k8s/base/hpa.yaml) |
| Dashboards | Grafana sidecar hot-loads a ConfigMap of JSON dashboards: inference-overview, latency-breakdown, model-quality, gpu-utilization | [observability/grafana/dashboards/](../observability/grafana/dashboards/) |
| Alerts | PrometheusRules: `HighP95Latency`, `HighErrorRate`, `HighGpuUtilization`, `HighGpuMemoryUsage` | [observability/prometheus/rules/](../observability/prometheus/rules/), runbook in [incident-runbook.md](incident-runbook.md) |
| Traces | gateway + model-server export OTLP/HTTP to `otel-collector:4318`; the collector batches and forwards to a trace backend | [k8s/base/otel-collector.yaml](../k8s/base/otel-collector.yaml), [observability/opentelemetry/collector.yaml](../observability/opentelemetry/collector.yaml) |
| Logs | promtail tails container logs into Loki; queryable in Grafana by namespace/pod | [observability/loki/values.yaml](../observability/loki/values.yaml) |

The metrics contract worth knowing: counters render with the `_total` suffix
(`inferenceops_request_count_total`), and every prediction carries a
`model_version` label, which is how canary checks and the benchmark separate
stable from candidate traffic. The full signal-by-signal treatment (SLI/SLO
contract, dashboards, a worked latency-incident walkthrough) is in
[observability.md](observability.md).

---

## 5. Autoscaling

Two layers scale different things:

- **Pod autoscaling (active):** the HPA in [k8s/base/hpa.yaml](../k8s/base/hpa.yaml)
  scales model-server on cpu 70% + memory 80% (2..10 replicas); the local overlay
  [k8s/overlays/local/patch-hpa-custom-rps.yaml](../k8s/overlays/local/patch-hpa-custom-rps.yaml)
  adds the external `inferenceops_rps` metric (~30 RPS per replica) to the same
  HPA. Request rate is the signal that actually tracks serving load; cpu/memory
  are co-signals and the graceful-degradation fallback. A KEDA `ScaledObject`
  ([k8s/overlays/local/scaledobject-model-server.yaml](../k8s/overlays/local/scaledobject-model-server.yaml))
  is the opt-in alternative. One scaler per Deployment, never both.
- **Node autoscaling (EKS-only):** Karpenter provisions right-sized nodes when
  pods go `Pending`. It is realized as the Karpenter submodule of the EKS
  Terraform module ([infra/terraform/modules/eks/](../infra/terraform/modules/eks/)),
  gated by `enable_karpenter`, and cannot run on kind (no cloud provider).

The full comparison (HPA vs KEDA vs cluster-autoscaler vs Karpenter, the
external-metric APIService exclusivity, GPU autoscaling caveats) is in
[autoscaling-strategy.md](autoscaling-strategy.md).

---

## 6. AWS / EKS topology

Production runs the same workloads on EKS. Terraform roots under
[infra/terraform/envs/](../infra/terraform/envs/) (`dev`, `staging`, `prod`)
wire seven modules from [infra/terraform/modules/](../infra/terraform/modules/):

```
   internet
      │
      ▼
   ┌─────┐   public subnets (3 AZ)          ┌──────────────────────────────┐
   │ ALB │   tag kubernetes.io/role/elb=1   │  ECR  gateway, model-server, │
   └──┬──┘                                  │       worker (IMMUTABLE,     │
      │ forward :80 ──▶ :8080 (IP targets)  │       scan-on-push)          │
      ▼                                     └──────────────▲───────────────┘
   ┌───────────────────────────────────────┐               │ image pull via NAT GW
   │  EKS cluster  (Kubernetes 1.33)       │───────────────┘
   │  private subnets (3 AZ)               │
   │  • cpu node group  m6i.xlarge (2..6)  │── IRSA ──▶ IAM role ──▶ AmazonS3ReadOnlyAccess
   │  • gpu node group  g5.xlarge  (0..4)  │                              │
   │    taint nvidia.com/gpu=present:NoSc… │                              ▼
   │  • Karpenter (optional)               │                        ┌───────────┐
   └───────────────────────────────────────┘                        │ S3 bucket │
                                                                    │ artifacts │
   CloudWatch: per-component log groups + ALB 5xx / p95 alarms      └───────────┘
   (monitoring module)
```

- **vpc**: public + private subnets across three AZs, one NAT gateway per AZ,
  Load-Balancer-Controller subnet tags.
- **eks**: the cluster, a CPU node group that always exists, an optional GPU
  node group (tainted so CPU workloads stay off it), and optional Karpenter.
- **ecr / s3**: immutable scanned image repos; a versioned, encrypted,
  fully-private artifact bucket.
- **iam**: the IRSA role the model-server pod assumes for S3 reads (no static
  keys in the cluster).
- **alb / monitoring**: the internet-facing L7 edge and CloudWatch alarms.

The module-by-module walkthrough, the offline `terraform plan`, IRSA wiring,
the GPU node group, and cost are all in [aws-deployment.md](aws-deployment.md).

---

## 7. Repository layout

| Path | Contents |
|---|---|
| `apps/gateway/` | Auth + rate-limit edge (FastAPI). Dockerfile at `apps/gateway/Dockerfile`. |
| `apps/model-server/` | The serving app: batching, predictor, metrics, storage abstraction. |
| `apps/worker/` | Offline batch inference, drift job, report generator. |
| [k8s/base/](../k8s/base/) | Kustomize base: deployments, services, ingress, HPA, PDB, ConfigMap/Secret, OTel collector. |
| [k8s/overlays/](../k8s/overlays/) | `local` (kind), `staging`, `prod` (GPU-ready, hard anti-affinity, `:prod` tags). |
| [k8s/canary/](../k8s/canary/) | Canary tier + the nginx canary-weight ingress pair. |
| [infra/terraform/modules/](../infra/terraform/modules/) | `vpc`, `eks`, `ecr`, `s3`, `iam`, `alb`, `monitoring`. |
| [infra/terraform/envs/](../infra/terraform/envs/) | `dev`, `staging`, `prod` roots (offline-plan machinery in each). |
| [observability/](../observability/) | Prometheus values + alert rules, prometheus-adapter, Grafana dashboards, Loki, OTel collector config. |
| `models/training/` | Training + evaluation (`train.py`, `evaluate.py`, `dataset.py`). |
| `models/registry/` | register / approve / promote CLIs + the registry store. |
| `models/artifacts/` | Local model artifacts (`fraud_detector/`, `fraud_detector_canary/`). |
| [scripts/](../scripts/) | Deploy, canary, upload, load-test, verification scripts (`deploy_local.sh`, `verify_k8s.sh`, `tf_mock.sh`, …). |
| [.github/workflows/](../.github/workflows/) | CI, image build+push, eval gate, model upload. |
| [serving/](../serving/) | FastAPI vs vLLM vs KServe vs Ray Serve: tradeoffs + per-backend guides. |
| [evals/reports/benchmark.md](../evals/reports/benchmark.md) | The canonical benchmark table (real local numbers + cost model). |
| [load_tests/](../load_tests/) | k6 scenarios (`smoke`, `spike`, `soak`, `stress`) + the locust file. |
| [Makefile](../Makefile), [docker-compose.yml](../docker-compose.yml) | Developer entry points (`make deploy-local`, `make test`, compose observability stack). |

---

## See also

- [kubernetes-ml-workloads.md](kubernetes-ml-workloads.md): the manifest-by-manifest "why" behind every Kubernetes field.
- [aws-deployment.md](aws-deployment.md): VPC/EKS/ECR/S3/IAM/ALB Terraform walkthrough, IRSA, offline plan, GPU node group, cost.
- [autoscaling-strategy.md](autoscaling-strategy.md): HPA vs KEDA vs Karpenter in depth.
- [observability.md](observability.md): the metrics/traces/logs stack, SLI/SLO, and incident walkthrough.
- [cost-analysis.md](cost-analysis.md): CPU vs GPU economics and measured cost-per-1k.
- [incident-runbook.md](incident-runbook.md): alert-by-alert response procedures.
- [serving/fastapi/deployment.md](../serving/fastapi/deployment.md): the "how do I run it" deployment guide (kind + EKS + canary).
