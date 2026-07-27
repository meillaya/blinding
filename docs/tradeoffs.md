# Tradeoffs

Every platform inherits its tradeoffs whether it names them or not. This page
names six of them: the options, when each wins, and what this repo chose and
why. The short version also lives in the README's Tradeoffs section; this page
is the full argument.

## 1. EKS vs SageMaker

The control-vs-managed axis.

| | EKS (chosen) | SageMaker |
|---|---|---|
| **You operate** | The Kubernetes layer: node groups, ingress, autoscaling, observability | Almost nothing; endpoints, scaling, and registries are managed |
| **You get** | One deployment target for local (kind) and cloud, any workload shape, full manifest control | Fast time-to-endpoint, built-in model registry and experiments, AWS-native auth |
| **Lock-in** | Low: manifests are portable, Terraform is the only AWS-specific layer | High: the endpoint, registry, and SDK shapes are AWS-specific |
| **Cost model** | You pay for nodes; autoscaling savings are yours to keep | Per-instance-hour plus managed overhead; scale-to-zero on serverless endpoints |

**When SageMaker wins:** a small team that wants an endpoint this week, lives
entirely in AWS, and does not need custom serving logic. **When EKS wins:** you
already run Kubernetes, you want the same manifests on a laptop and in the
cloud, or your serving path is bespoke (custom batching, a custom gateway, a
canary you control).

**What this repo chose: EKS.** The whole point of the project is a serving
platform you can defend line by line: the same kustomize base deploys to kind
(`make deploy-local`) and to EKS
([`infra/terraform/envs/`](../infra/terraform/envs/)), the canary is an nginx
annotation you can read, and the autoscaling signals are explicit. SageMaker
would have hidden exactly the layer this project exists to show. The cost is
real: node groups, ingress, Prometheus, and cert-manager are all ours to run.
The [AWS deployment doc](./aws-deployment.md) is the receipt for that work.

## 2. FastAPI vs KServe vs Ray Serve

The serving-framework axis. The full comparison table lives in
[`serving/README.md`](../serving/README.md); this is the decision behind it.

| | FastAPI (chosen, implemented) | KServe (documented) | Ray Serve (scaffold only) |
|---|---|---|---|
| **Best at** | Custom or classical models, bespoke schemas, minimal moving parts | A fleet of models under one Kubernetes-native contract (InferenceService CRD), scale-to-zero | Python model DAGs with a built-in queue-size autoscaler |
| **Runtime cost** | One uvicorn process | CRDs + a control plane + a predictor runtime | A Ray cluster to operate |
| **Autoscaling** | HPA on external RPS + cpu/memory | Knative KPA concurrency / scale-to-zero, or HPA | Replica autoscaler on `target_ongoing_requests` |
| **In this repo** | Implemented + benchmarked | Standard mode, no Istio, two ISVCs Ready | `@serve.deployment` scaffold, not wired anywhere |

**When KServe wins:** many models, one team, and you want rollout, canary, and
scale-to-zero standardized across all of them. **When Ray Serve wins:** the
model is really a graph (retriever, then reranker, then generator) and you want
composition plus autoscaling in one framework. **When FastAPI wins:** one model
with a custom contract and no need for a control plane.

**What this repo chose: FastAPI for the default model.** The fraud detector is
a scikit-learn classifier on CPU with a 20-feature schema; vLLM's PagedAttention
and KServe's control plane solve problems it does not have. A small FastAPI app
keeps the moving parts minimal and fully benchmarked
([`serving/fastapi/benchmark.md`](../serving/fastapi/benchmark.md)). The heavier
backends stay documented (and KServe is actually installed and serving on the
kind cluster) so the choice is reversible: if the workload grows into a fleet
or a GPU LLM, the manifests and the reasoning are already here.
[`serving/fastapi/deployment.md`](../serving/fastapi/deployment.md) carries the
complete deployment guide.

## 3. HPA vs KEDA vs Karpenter

Two different axes that people conflate: **pod** scaling (how many replicas)
versus **node** scaling (how much cluster). The full treatment, with install
commands and the mutual-exclusion gotcha, is in
[`docs/autoscaling-strategy.md`](./autoscaling-strategy.md).

| | HPA (chosen) | KEDA (alternative, installed) | Karpenter (node layer, EKS-only) |
|---|---|---|---|
| **Scales** | Pods | Pods (event-driven) | Nodes |
| **Signal** | cpu, memory, or an external metric via prometheus-adapter | Prometheus query, queue depth, Kafka lag, cron, dozens of scalers | Pending pods that need a home |
| **Lag** | 15s metric sync + pod cold start | Similar (it drives an HPA-shaped object) | Minutes (provisioning a node) |
| **Runs here** | Yes, on kind and EKS | Yes, ScaledObject installed on kind | Terraform reference only; kind has no cloud provider |

**When KEDA wins:** the scaling signal is an event source the HPA cannot see
(a Kafka lag, a queue depth, a schedule). **When Karpenter wins:** pod scaling
runs out of room on the nodes, and you want right-sized nodes provisioned
on demand instead of a static node group. **When plain HPA wins:** the signal
is request rate or resource use, and the cluster already has headroom.

**What this repo chose: HPA on the external `inferenceops_rps` metric, with
cpu and memory as co-signals.** The gateway sheds about 95% of a vertical
burst, so model-server CPU barely moves under load; scaling on CPU alone would
under-react. The external metric (gateway → Prometheus → prometheus-adapter →
HPA) reacts to real demand, at the price of a 30-50s lag the spike test is
sized to absorb. KEDA is installed as the documented alternative (the two
share one external-metrics APIService, so you run exactly one of them), and
Karpenter is wired in the EKS Terraform for the node-level story that kind
cannot exercise.

## 4. vLLM vs TGI vs Triton

The GPU serving axis. Only vLLM is built into this repo; TGI and Triton appear
here because an interviewer will ask why not them.

| | vLLM (chosen) | TGI | Triton Inference Server |
|---|---|---|---|
| **Core strength** | PagedAttention + continuous batching for LLM throughput | Hugging Face models, token streaming, production hardening | Multi-framework model repositories, ensemble DAGs, dynamic batching across backends |
| **API** | OpenAI-compatible out of the box | HF-native + OpenAI-compatible endpoints | gRPC/HTTP with a model-repository layout |
| **Sweet spot** | High-throughput OpenAI-shaped LLM serving | HF-centric LLM deployments | Mixed fleets (TensorRT, ONNX, PyTorch, TF) behind one server |
| **Operational weight** | Medium (GPU node + device plugin + shm sizing) | Medium | High (model repository, config.pbtxt, backend images) |

**When TGI wins:** your models and tooling are already Hugging Face-native.
**When Triton wins:** you serve a heterogeneous fleet or need server-side
ensembles across frameworks. **When vLLM wins:** you want maximum
tokens-per-second from a GPU behind an OpenAI-compatible API.

**What this repo chose: vLLM as the GPU LLM backend**
([`serving/vllm/`](../serving/vllm/)). The gateway already reserves the
OpenAI-compatible routes (`/v1/completions`, `/v1/chat/completions`), and vLLM
serves exactly those as a drop-in, pinned at `vllm/vllm-openai:v0.25.1`.
PagedAttention and continuous batching squeeze far more throughput out of a
GPU than a hand-rolled loop, and the EKS module provisions the `g5` node group
it runs on. Because this host has no GPU, vLLM is build- and doc-verified, not
runtime-measured; a small CPU LLM (llama.cpp) stands in locally so the routes
still work end to end.

## 5. Canary vs blue-green

The release-strategy axis.

| | Canary (chosen) | Blue-green |
|---|---|---|
| **Cutover** | Gradual: 90/10 split, then promote to 100% | Instant: flip from blue to green |
| **Blast radius of a bad deploy** | 10% of traffic, until the check rolls it back | 100%, the moment you flip |
| **Rollback** | Shift the weight back (or let the auto-check do it) | Flip back; the old stack stayed warm |
| **Infra cost** | One extra Deployment + an annotated Ingress | Two full environments, warm |
| **What you can observe before commit** | Latency, errors, and eval quality on real traffic, per version | Nothing on real traffic; the green side only saw mirrored or synthetic load |

**When blue-green wins:** stateful schema migrations where two versions cannot
share a database, or a cutover that must be instantaneous. **When canary
wins:** you want the new version proven on a slice of real traffic before it
sees all of it.

**What this repo chose: an nginx-ingress 90/10 canary with automated,
fail-closed rollback.** The canary is pure annotation
([`k8s/canary/`](../k8s/canary/)): a canary Ingress with `canary-weight: 10`
sends 10% to the candidate, and the `model_version` metric label is the
discriminator that makes candidate-vs-stable comparison possible.
[`scripts/canary_check.sh`](../scripts/canary_check.sh) runs every 30s and
rolls back on candidate p95 above stable × 1.20, candidate error ratio above
1%, or eval regression above 3%; any check error or a Prometheus outage longer
than 60s also rolls back (fail-closed, never fail-open). Promotion stays a
deliberate human step (`make promote`) once the observation window is clean.
The cost of skipping a service mesh: no per-request mesh features, and
KServe's own traffic-splitting canary needs its Serverless mode (documented in
[`serving/kserve/canary.yaml`](../serving/kserve/canary.yaml)).

## 6. devenv vs raw Nix flakes

The reproducible-toolchain axis.

| | devenv (chosen) | Raw flakes |
|---|---|---|
| **You write** | A declarative `devenv.nix` (+ `devenv.yaml` / lock) | `flake.nix` with `devShells`, `packages`, overlays by hand |
| **You get** | Packages, languages, tasks, and a shell in one high-level file; batteries for common toolchains | Total control, no abstraction over nixpkgs |
| **Onboarding** | `devenv shell` and the whole toolchain appears | `nix develop`, after you have written the shell yourself |
| **Escape hatch** | Drop to the underlying flake; devenv wraps one | You are already there |

**When raw flakes win:** you need an exotic package set or an overlay story
that a higher-level tool would fight. **When devenv wins:** the goal is a
reproducible developer environment for a team, fast.

**What this repo chose: devenv** ([`devenv.nix`](../devenv.nix), with
`devenv.yaml` and a lock file). It provides exactly the tools the host lacks
(kind, k6, helm, kustomize, uv, a pinned Python 3.12, shellcheck, actionlint,
hadolint) and wraps a flake underneath, so you keep the escape hatch without
writing the shell by hand. The split matters: `devenv.yaml` carries the
non-Nix configuration and `devenv.nix` the package set, which is the layout the
devenv tooling expects. The payoff is that every script in this repo runs the
same way on a fresh checkout as on the machine that wrote it.

## See also

- [`serving/README.md`](../serving/README.md): the FastAPI vs vLLM vs KServe vs Ray Serve tradeoff table.
- [`docs/autoscaling-strategy.md`](./autoscaling-strategy.md): HPA vs KEDA vs Karpenter in depth, GPU caveats included.
- [`docs/cost-analysis.md`](./cost-analysis.md): the numbers behind the CPU-vs-GPU and autoscaling-savings claims.
- [`docs/aws-deployment.md`](./aws-deployment.md): what operating the EKS choice actually costs.
- [`docs/interview-deep-dives.md`](./interview-deep-dives.md): these tradeoffs as interview Q&A.
