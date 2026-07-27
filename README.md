# InferenceOps

An end-to-end MLOps platform that trains, serves, and operates a fraud-detection
model the way a production team would. It pairs a FastAPI model server and an API
gateway with Kubernetes deployment, autoscaling, canary release with automated
rollback, a model registry, full observability, and load testing with real
benchmark numbers. **The core platform runs entirely locally on kind**: the
model server, gateway, canary with automated rollback, HPA/KEDA autoscaling,
Prometheus/Grafana/OTel/Loki observability, k6/locust load testing, and the
serving backends all work on a laptop with no AWS account, credentials, or
spend. An optional AWS/Terraform layer under
[`infra/terraform/`](infra/terraform) shows the same manifests targeting EKS
for AWS-readiness, but nothing in the local platform depends on it.

The default model is a scikit-learn fraud classifier served on CPU. The serving
layer is pluggable: vLLM (GPU LLM), KServe, and Ray Serve are documented alongside
the implemented FastAPI backend so the tradeoffs are explicit. A small CPU LLM
(llama.cpp) stands in for the GPU path so the OpenAI-compatible routes work
without a GPU.

## What It Does

InferenceOps answers one question on every request: *is this transaction
fraudulent?* A client posts 20 numeric features to `/v1/predict` and gets back a
label, a fraud score, and the model version that produced it. Around that simple
contract the platform adds everything production serving needs:

- **Serving.** A FastAPI model server ([`apps/model-server`](apps/model-server))
  loads the model from a storage backend, batches incoming requests, exposes
  Prometheus metrics, and shuts down gracefully on SIGTERM.
- **Edge.** An API gateway ([`apps/gateway`](apps/gateway)) sits in front: API-key
  auth, token-bucket rate limiting, an OpenTelemetry span per request, and proxy
  routes to the model server plus the reserved LLM routes
  (`/v1/completions`, `/v1/chat/completions`).
- **Model lifecycle.** Train ([`models/training`](models/training)), register and
  promote ([`models/registry`](models/registry)), store behind one interface that
  speaks both local disk and S3, and gate every promotion on accuracy, latency,
  and regression evals.
- **Release safety.** An nginx-ingress 90/10 canary with an automated,
  fail-closed health check ([`scripts/canary_check.sh`](scripts/canary_check.sh))
  that rolls back on latency, error-rate, or eval regressions. Promotion stays a
  deliberate human step.
- **Autoscaling.** HPA on CPU, memory, and the external request-rate metric, with
  KEDA and Karpenter documented for the cluster-level story.
- **Observability.** Prometheus, Grafana (four dashboards), OpenTelemetry traces,
  Loki logs, alert rules, SLI/SLO definitions, and an incident runbook.
- **Load testing.** k6 (smoke, spike, soak, stress) and Locust, feeding a
  benchmark report with real local numbers.
- **Batch + drift.** An offline worker ([`apps/worker`](apps/worker)) runs batch
  inference and a population-stability-index (PSI) drift job over scored traffic.

## Architecture

The request path is a short spine with an observability sidecar and a canary
split hanging off it:

```
  client (curl / k6 / locust)
     |  HTTPS  +  X-API-Key
     v
  +----------------------------------+
  | ingress-nginx                    |      host inferenceops.local
  | :8080 (kind)  /  ALB (EKS)       |
  +---------------+------------------+
                  |
        canary split (nginx canary annotations)
        90% stable  ----+----  10% candidate
                  |     |
                  v     v
  +----------------------------------+     +----------------------------------+
  | gateway  (FastAPI)               |     | observability sidecar path       |
  |  auth: X-API-Key  -> 401         |     |                                  |
  |  token-bucket rate-limit -> 429  |     |  gateway + model-server emit     |
  |  OTel span per request           |---->|   OTLP traces -> OTel collector  |
  |  proxies /v1/predict, /v1/batch  |     |   /metrics    -> Prometheus      |
  |  LLM: /v1/completions, chat      |     |   stdout logs -> Loki (promtail) |
  +---------------+------------------+     +----------------+-----------------+
                  |  internal HTTP                          |
                  v                                          v
  +----------------------------------+     +----------------------------------+
  | model-server  (FastAPI)          |     | OTel collector -> traces/metrics |
  |  dynamic batching                |     | Prometheus     -> alerts, Grafana|
  |  Prometheus metrics              |     | Loki           -> log queries    |
  |  /v1/predict /v1/batch /v1/models|     | Grafana        -> 4 dashboards   |
  |  /healthz /readyz /metrics       |     +----------------------------------+
  +---------------+------------------+
                  |  load at startup
                  v
  +----------------------------------+
  | model artifact                   |
  |  models/artifacts  (Local)       |     fraud-detector @ 1.0.0
  |  s3://bucket       (S3 + IRSA)   |     scikit-learn, 20 features
  +----------------------------------+

  autoscaling: HPA (cpu / memory / external inferenceops_rps) + KEDA;
               Karpenter adds nodes on EKS
  release:     nginx 90/10 canary -> canary_check.sh (fail-closed, 30s) ->
               make promote / make rollback
```

**Component responsibilities.**

- **ingress-nginx** terminates TLS and owns the canary weight. The base ingress
  sends 90% of traffic to the stable gateway; a canary ingress with the
  `canary-weight: 10` annotation sends the remaining 10% to the candidate. No
  service mesh; the split is pure nginx annotation.
- **gateway** is the trust and traffic boundary. It rejects bad API keys (401),
  sheds load past the per-key token bucket (429 with `Retry-After`), wraps every
  request in a trace span, and forwards to the model server. It also owns the
  OpenAI-compatible LLM routes and the external `inferenceops_rps` metric the HPA
  scales on.
- **model-server** does the inference. The `DynamicBatcher` coalesces requests up
  to a max batch size or wait time and fans results back; metrics
  (`inferenceops_request_count_total`, `inferenceops_request_duration_seconds`,
  `inferenceops_model_load_seconds`, and friends) are labeled by `model_version`,
  which is exactly the discriminator the canary check uses to compare candidate
  against stable.
- **model artifact** lives behind the `ModelStorage` interface. Locally it is
  `models/artifacts`; on EKS the same code reads from S3 through an IRSA-bound
  service account.
- **observability sidecar path.** Both apps export OTLP traces to the OpenTelemetry
  collector and expose `/metrics` for Prometheus to scrape; Promtail ships their
  stdout to Loki. Prometheus drives the alert rules and the four Grafana
  dashboards (inference overview, latency breakdown, model quality, GPU
  utilization).

On AWS the same spine runs on EKS, with managed services replacing the local
ones:

```
  Route53 / ALB
     |
     v
  EKS cluster  (infra/terraform/modules/eks)
   |- node group: m6i (CPU model-server + gateway)
   |- node group: g5  (GPU, vLLM LLM backend)
   |- HPA + KEDA + Karpenter
   |- nginx-ingress canary
   |
   +-- ECR  (gateway, model-server, worker images; scan-on-push, immutable tags)
   +-- S3   (model artifacts; model-server reads via IRSA, no static keys)
   +-- IAM  (IRSA role per service; AmazonS3ReadOnlyAccess for model-server)
   +-- VPC  (public + private subnets, NAT per AZ, ELB role tags)
```

See [`docs/architecture.md`](docs/architecture.md) for the full data-flow
walkthrough and [`docs/aws-deployment.md`](docs/aws-deployment.md) for the VPC,
EKS, ECR, S3, IAM, and ALB details.

## Features

- **Fraud-detection model server.** FastAPI + uvicorn, Pydantic-validated
  requests, dynamic batching, readiness vs liveness probes (`/readyz` stays 503
  until the model loads), graceful SIGTERM drain, Prometheus instrumentation.
- **API gateway.** API-key auth, per-key token-bucket rate limiting, OTel tracing,
  request proxying, and OpenAI-compatible LLM routes with a pluggable backend
  (llama.cpp/ollama on CPU, vLLM on GPU).
- **Storage abstraction.** One `ModelStorage` interface over local disk and S3
  (boto3), with typed errors and an artifact-identity invariant so a partial
  upload never publishes a phantom model.
- **Model registry + eval gates.** Register, promote, approve, and render model
  cards; golden-dataset accuracy, latency, and regression gates block a bad
  promotion in CI and at the canary.
- **Canary + automated rollback.** nginx-ingress 90/10 split;
  [`scripts/canary_check.sh`](scripts/canary_check.sh) runs every 30s, is
  fail-closed on any check error, rolls back on p95 +20%, errors >1%, or eval
  regression >3%, and falls back conservatively if Prometheus is unavailable.
- **Autoscaling.** HPA on CPU, memory, and the external `inferenceops_rps` metric
  (prometheus-adapter), fast scale-up/scale-down behavior, KEDA for event-driven
  scaling, Karpenter documented for node provisioning on EKS.
- **Observability.** kube-prometheus-stack, four Grafana dashboards provisioned
  via a sidecar ConfigMap, alert rules (high p95 latency, high error rate, GPU
  utilization, GPU memory), OTel collector, Loki logs, SLI/SLO definitions, and an
  incident runbook with a sample postmortem.
- **Load testing + benchmarks.** k6 smoke/spike/soak/stress and Locust, plus a
  benchmark generator that turns the run artifacts and live Prometheus data into a
  costed table.
- **Serving backends.** FastAPI (implemented), vLLM (GPU, documented), KServe
  (Standard mode, no Istio, documented), Ray Serve (queue-size autoscaling,
  scaffold). See [`serving/README.md`](serving/README.md).
- **Offline batch + drift.** A worker runs batch inference with a report generator
  and a PSI drift job that alerts when scored traffic shifts from training.
- **Reproducible toolchain + CI.** uv workspaces, ruff, basedpyright, pytest with
  coverage gates (model-server >=80%, gateway >=70%), multi-stage non-root Docker
  images, and GitHub Actions (lint, typecheck, test, docker build, Trivy scan,
  gated ECR push, eval gate). [`devenv.nix`](devenv.nix) pins the developer
  environment.

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/), a rootless Docker daemon, and
`kind` + `kubectl` for the cluster path. `devenv.nix` provides the rest (k6,
hadolint, kind). All operator commands go through the self-documenting
[`Makefile`](Makefile); run `make` with no arguments to list them.

**1. Run the model server locally** (no cluster, plain uvicorn on `:8080`):

```bash
make run-local
```

Then, in another shell:

```bash
curl -s -X POST http://localhost:8080/v1/predict \
  -H 'Content-Type: application/json' \
  -d '{"features":[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0]}'
# {"prediction":0,"score":0.034,"model_id":"fraud-detector","model_version":"1.0.0"}
```

`/healthz`, `/readyz`, `/metrics`, and `/v1/models` are served from the same
process. Note that `make run-local` binds host port `:8080`, which the kind
cluster's ingress also uses; run one or the other, not both at once.

**2. Deploy the full stack to a local kind cluster** (gateway, model-server,
ingress, HPA, and kube-prometheus-stack):

```bash
make deploy-local
```

This builds both images, creates the `inferenceops` kind cluster, installs
ingress-nginx and kube-prometheus-stack (Prometheus + Grafana), and applies
[`k8s/overlays/local`](k8s/overlays/local) (gateway, model-server, HPA, OTel
collector). The four custom Grafana dashboards and Loki are provisioned
separately (see [`docs/observability.md`](docs/observability.md)). Predict through
the ingress; `deploy_local.sh` sets the local gateway API key to `test-key`:

```bash
curl -s -X POST http://localhost:8080/v1/predict \
  -H 'Host: inferenceops.local' \
  -H 'X-API-Key: test-key' \
  -H 'Content-Type: application/json' \
  -d '{"features":[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0]}'
```

**3. Run the test suite** (260 tests, with coverage):

```bash
make test
```

Other useful entry points: `make lint`, `make typecheck`, `make eval` (the
accuracy/latency/regression gates), `make train` (regenerate the artifact),
`make load-test-smoke`, and the canary lifecycle `make deploy-canary
MODEL_VERSION=v4` / `make promote` / `make rollback`.

## AWS Deployment

> **Optional.** Everything below this point is the optional AWS/Terraform layer.
> The platform runs fully on local kind without any of it. See the
> [Quickstart](#quickstart) above for the local-only path.

The local manifests are a kustomize overlay of the same base used for AWS, so the
platform deploys to EKS through Terraform in [`infra/terraform`](infra/terraform).
Modules cover the whole topology: [`vpc`](infra/terraform/modules/vpc),
[`eks`](infra/terraform/modules/eks), [`ecr`](infra/terraform/modules/ecr),
[`s3`](infra/terraform/modules/s3), [`iam`](infra/terraform/modules/iam),
[`alb`](infra/terraform/modules/alb), and
[`monitoring`](infra/terraform/modules/monitoring), composed per environment under
[`envs/{dev,staging,prod}`](infra/terraform/envs).

```bash
cd infra/terraform/envs/prod
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

Highlights:

- **IRSA, not static keys.** The `iam` module creates a
  `${name}-model-server-irsa` role trusting the `inferenceops:model-server`
  service account with `AmazonS3ReadOnlyAccess`. The model server reads artifacts
  from S3 with pod-scoped credentials; no long-lived AWS keys touch the cluster.
- **GPU node group.** The EKS module provisions a `g5` node group for the vLLM LLM
  backend alongside the CPU `m6i` group; the NVIDIA device plugin advertises
  `nvidia.com/gpu` to the scheduler.
- **ECR.** Three repositories (gateway, model-server, worker) with scan-on-push and
  immutable tags; CI pushes are gated ([`.github/workflows`](.github/workflows)).
- **Offline plan.** `terraform plan` runs without live AWS credentials in CI by
  pointing the provider at a local moto endpoint, so infrastructure changes are
  reviewed before they touch a real account.

The complete walkthrough (VPC, subnets, IRSA binding at deploy time, the GPU
group, and cost) is in [`docs/aws-deployment.md`](docs/aws-deployment.md) and
[`infra/terraform/envs/prod/README.md`](infra/terraform/envs/prod/README.md); the
manifest field rationale is in
[`docs/kubernetes-ml-workloads.md`](docs/kubernetes-ml-workloads.md).

## Benchmarks

Real local numbers from k6 and Locust runs against the kind cluster, with live
Prometheus supplying replica counts and model-load time. Cost assumes one full
node billed per replica (CPU `$0.384/hr` m6i.2xlarge, GPU `$1.212/hr` g5.2xlarge,
us-east-1 on-demand); `cost/1k = replicas * node_$hr / (RPS * 3600) * 1000`.

| scenario | replicas | RPS | p95 (ms) | p99 (ms) | error rate | cost / 1k reqs | cold start (s) | model load (s) | GPU util | throughput / replica |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline CPU (smoke, 2 VUs) | 2 | 37.1 | 54.4 | 54.8 | 0.00% | $0.0058 | 7 | 0.844 | documented (no GPU) | 18.6 |
| HPA enabled (spike, 70 VUs) | 6 | 2897.5 | 65.6 | 396.1 | 93.04% | $0.0002 | 7 | 0.844 | documented (no GPU) | 482.9 |
| soak (8 VUs, 90 s) | 5 | 137.9 | 60.6 | 62.4 | 0.00% | $0.0039 | 7 | 0.844 | documented (no GPU) | 27.6 |
| locust (50 users, think 0.5-1.5 s) | 2 | 45.4 | 59 | 72 | 0.00% | $0.0047 | 7 | 0.844 | documented (no GPU) | 22.7 |
| canary candidate (2.0.0, 10% split) | 1 | 2.7 | 73.8 | 74.8 | 0.00% | $0.0390 | 7 | 0.827 | documented (no GPU) | 2.7 |
| vLLM / GPU LLM (documented) | 1 (documented) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) |

Read the rows with two caveats. The spike row's 93% error rate is the *intended*
signal, not a failure: the gateway sheds most of a vertical burst while the HPA
scales model-server 2 to 6, and a follow-up smoke returns to p95 ~56ms at 0%
errors. The canary row's high cost-per-1k is an artifact of measuring a 10%
traffic slice (2.7 RPS) against a whole billed node. The GPU row is documented,
not measured: this host has no GPU, so the DCGM series are absent. Methodology and
raw artifacts are in [`evals/reports/benchmark.md`](evals/reports/benchmark.md) and
[`serving/fastapi/benchmark.md`](serving/fastapi/benchmark.md).

## Failure Scenarios Tested

Six failure modes are exercised; four run end-to-end on the local kind cluster and
two are documented procedures because a single-node CPU cluster cannot reproduce
them. The executable suite lives in
[`scripts/failure_scenarios`](scripts/failure_scenarios) and runs through
`scripts/failure_scenarios/run_failure_scenarios.sh`.

| scenario | kind | what proves it |
|---|---|---|
| model-server crash | executable | kill the server pod; liveness probe restarts it and the high-error-rate alert fires |
| bad model deploy | executable | a degraded model trips the eval regression gate and the canary auto-rolls back |
| latency regression | executable | candidate p95 above stable x 1.20 triggers fail-closed rollback in `canary_check.sh` |
| traffic spike | executable | a vertical burst drives HPA scale-up; latency and errors recover within SLO |
| GPU memory pressure | documented | procedure for DCGM memory alerts and GPU-backed pod eviction (needs a GPU node) |
| node failure | documented | procedure for node drain, pod rescheduling, and Karpenter replacement (needs >1 node) |

The incident response side is in [`docs/incident-runbook.md`](docs/incident-runbook.md),
which maps each alert to a diagnosis and mitigation path with a sample postmortem.

## Tradeoffs

The platform makes its tradeoffs explicit rather than inheriting them by accident.
The short version:

- **Custom FastAPI over a model server framework** for the default model. A
  scikit-learn classifier on CPU does not need vLLM's PagedAttention or KServe's
  control plane; a small FastAPI app keeps the moving parts minimal and fully
  benchmarked. The heavier backends stay documented so the choice is reversible.
- **nginx-ingress canary over a service mesh.** Annotation-based 90/10 splitting
  with a fail-closed health check buys the rollback safety net without running
  Istio. The cost: no per-request mesh features, and KServe's traffic-splitting
  canary needs its Serverless mode.
- **External-RPS HPA over CPU-only scaling.** The gateway sheds ~95% of a burst,
  so model-server CPU barely moves under load; scaling on the external
  `inferenceops_rps` metric reacts to real demand. The trade is a metric path
  (gateway to Prometheus to adapter to HPA) with a 30-50s lag the spike test is
  sized to absorb.
- **Fail-closed rollback over fail-open.** Any error in the canary check rolls
  back rather than promoting, which occasionally rolls back a healthy canary when
  Prometheus blips, in exchange for never promoting on uncertain data.
- **Local + S3 behind one interface.** The same loader serves `models/artifacts`
  on a laptop and `s3://` on EKS via IRSA, at the cost of a storage seam both
  backends must honor.
- **CPU LLM stand-in.** llama.cpp makes the OpenAI routes runnable without a GPU;
  throughput is small (~4 tok/s) and the real target is vLLM on a `g5` node.

The full discussion is in [`docs/tradeoffs.md`](docs/tradeoffs.md), and the
per-backend serving comparison is in [`serving/README.md`](serving/README.md).

## Interview Deep Dives

Topics a staff candidate should be able to defend, each grounded in something this
repo actually does:

- **Autoscaling beyond CPU.** Why the external-RPS HPA, the metric-path lag, cold
  start versus model-load time, and how KEDA and Karpenter extend the story to
  events and nodes. See [`docs/autoscaling-strategy.md`](docs/autoscaling-strategy.md).
- **Canary release and fail-closed rollback.** How the `model_version` metric label
  makes candidate-vs-stable comparison possible, the 30s check interval, and the
  Prometheus-unavailable fallback.
- **Observability and SLOs.** The four dashboards, the alert-to-runbook contract,
  and SLI/SLO definitions that reuse the alert expressions verbatim so the two
  never disagree.
- **Cost of serving.** The cost-per-1k model behind the benchmark table, CPU versus
  GPU economics, and what autoscaling saves. See [`docs/cost-analysis.md`](docs/cost-analysis.md).
- **GPU serving is hard.** Why GPU autoscaling, cold starts, and memory pressure
  resist the CPU playbook, and what the documented GPU scenarios assume.
- **Drift detection.** PSI over scored traffic, the epsilon-clipping that keeps an
  in-distribution baseline at exactly zero, and the worker job that alerts on it.
- **Storage and model identity.** The artifact-identity invariant, upload ordering
  (bytes first, metadata last), and IRSA pod permissions on EKS. See
  [`docs/security.md`](docs/security.md).

The collected deep-dives are in [`docs/interview-deep-dives.md`](docs/interview-deep-dives.md).

## Repository Layout

```
apps/            gateway, model-server, worker (uv workspace members)
models/          training, registry, artifacts
serving/         fastapi (implemented), vllm, kserve, ray-serve + tradeoff README
k8s/             base manifests + overlays/{local,staging,prod} + canary
infra/terraform  modules/{vpc,eks,ecr,s3,iam,alb,monitoring} + envs/{dev,staging,prod}
observability/   prometheus rules, grafana dashboards, opentelemetry, loki
load_tests/      k6 (smoke/spike/soak/stress) + locust
evals/reports    generated load-test + benchmark artifacts
scripts/         deploy, canary, eval, load-test, failure-scenario runners
docs/            architecture, deployment, autoscaling, observability, tradeoffs, ...
```

## See Also

- [`docs/kubernetes-ml-workloads.md`](docs/kubernetes-ml-workloads.md): the Kubernetes concepts behind the manifests, GPU scheduling included.
- [`docs/autoscaling-strategy.md`](docs/autoscaling-strategy.md): HPA vs KEDA vs Karpenter in depth.
- [`docs/incident-runbook.md`](docs/incident-runbook.md): alert-by-alert diagnosis and mitigation, with SLI/SLO and a sample postmortem.
- [`serving/README.md`](serving/README.md): the FastAPI vs vLLM vs KServe vs Ray Serve tradeoff table.
- [`serving/fastapi/deployment.md`](serving/fastapi/deployment.md): the complete kind and EKS deployment guide, canary included.
- [`evals/reports/benchmark.md`](evals/reports/benchmark.md): the canonical benchmark table and methodology.

> **Pending cross-links.** A few links above point at docs being written in the
> same pass as this README (todos 47, 48, 49, 50): `docs/architecture.md`,
> `docs/aws-deployment.md`, `docs/observability.md`, `docs/cost-analysis.md`,
> `docs/security.md`, `docs/tradeoffs.md`, `docs/interview-deep-dives.md`, and
> `scripts/failure_scenarios/`. They resolve once those todos land; every other
> link in this file points at a file that exists today.

## License

[MIT](LICENSE).
