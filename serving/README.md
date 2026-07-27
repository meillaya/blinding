# Serving backends

InferenceOps serves models through one of four backends. Only the FastAPI
model-server is implemented and run; the other three are documented (and, for
Ray Serve, scaffolded) so the platform's serving choices are explicit and
comparable. This page is the tradeoff map; each backend's directory carries its
own detail.

| Backend | Directory | Status |
|---|---|---|
| FastAPI model-server | [`fastapi/`](./fastapi/) | **Implemented** (`apps/model-server`, deployed by `k8s/`) |
| vLLM | [`vllm/`](./vllm/) | Documented (GPU LLM backend, todo 42) |
| KServe | [`kserve/`](./kserve/) | Documented (Standard mode, no Istio, todo 44) |
| Ray Serve | [`ray-serve/`](./ray-serve/) | Scaffold only, NOT run (todo 45) |

## Tradeoff table

| | FastAPI | vLLM | KServe | Ray Serve |
|---|---|---|---|---|
| **Runtime** | Custom Python: uvicorn + FastAPI (`apps/model-server`) | vLLM engine (PagedAttention, continuous batching) | Kubernetes control plane (InferenceService CRD) over a predictor runtime | Ray cluster + Serve replicas (Python-native) |
| **GPU / CPU** | CPU (the default fraud model); GPU possible but not its strength | GPU-first (built for LLM throughput) | Both, per predictor | Both, per deployment |
| **Autoscaling** | k8s HPA on external RPS (`inferenceops_rps`) + cpu/memory | HPA on GPU util / RPS; Karpenter provisions GPU nodes | Knative scale-to-zero / concurrency (KPA), or HPA | Built-in replica autoscaler on queue size (`target_ongoing_requests`) |
| **OpenAI-compatible** | No (custom `/v1/predict` schema) | Yes (ships `/v1/chat/completions`, `/v1/completions`) | Depends on the predictor runtime | Not by default (wrap it yourself) |
| **Complexity** | Low | Medium | High (CRDs + control plane) | Medium-high (operate a Ray cluster) |
| **In this repo** | Implemented + benchmarked | Documented | Documented | Scaffold only, not wired into CI/compose |

## When to choose each

**FastAPI** ([`fastapi/`](./fastapi/)): the default here. Pick it for custom or
classical models (the fraud detector is scikit-learn), CPU inference, a bespoke
request schema, and full control over the serving process with minimal moving
parts. It is the only backend with real benchmark numbers
([`fastapi/benchmark.md`](./fastapi/benchmark.md)) and a full deployment guide
([`fastapi/deployment.md`](./fastapi/deployment.md)).

**vLLM** ([`vllm/`](./vllm/)): pick it for high-throughput LLM inference on GPU.
PagedAttention and continuous batching squeeze far more tokens per second out of
a GPU than a hand-rolled loop, and the server is OpenAI-compatible out of the
box. The cost is a GPU node group (the EKS `g5.xlarge` group in
[`infra/terraform/modules/eks/`](../infra/terraform/modules/eks/)) and GPU-aware
autoscaling.

**KServe** ([`kserve/`](./kserve/)): pick it when you serve a fleet of models
and want a standardized Kubernetes-native contract (the InferenceService CRD)
for rollout, canary, and scale-to-zero across all of them. This repo documents
the **Standard mode** (Knative serving, no Istio dependency) to keep the mesh
complexity out. The price is the highest operational complexity of the four:
CRDs and a control plane to run and upgrade.

**Ray Serve** ([`ray-serve/`](./ray-serve/)): pick it for complex Python model
pipelines (DAGs of models, pre/post-processing graphs, dynamic batching) where
you want composition and a built-in queue-size autoscaler in one framework. In
this repo it is a **documented scaffold only**: [`ray-serve/deployment.py`](./ray-serve/deployment.py)
shows the `@serve.deployment` wrapping the fraud predictor with queue-size
autoscaling, and [`ray-serve/serve_config.yaml`](./ray-serve/serve_config.yaml)
mirrors it. Neither is run: `ray` is not a dependency, and no CI workflow or
docker-compose service starts it. `python -m py_compile serving/ray-serve/deployment.py`
passes because the file is valid Python (py_compile checks syntax, never
imports).

## Autoscaling, compared

The four backends scale on different signals, which is the crux of the choice:

- **FastAPI**: request rate. The HPA scales model-server off the external
  `inferenceops_rps` metric (averageValue 30 per replica), with cpu/memory as
  co-signals. See [`docs/autoscaling-strategy.md`](../docs/autoscaling-strategy.md).
- **vLLM**: GPU utilization and request rate, with Karpenter adding GPU nodes
  when pods go Pending.
- **KServe**: concurrency (Knative KPA) with scale-to-zero, or a plain HPA.
- **Ray Serve**: queue size. The autoscaler holds the average ongoing (queued)
  requests per replica at `target_ongoing_requests`, the conceptual cousin of the
  FastAPI external-RPS target. See the comments in
  [`ray-serve/deployment.py`](./ray-serve/deployment.py).

## See also

- [`docs/kubernetes-ml-workloads.md`](../docs/kubernetes-ml-workloads.md): the Kubernetes concepts behind the manifests, GPU scheduling included.
- [`docs/autoscaling-strategy.md`](../docs/autoscaling-strategy.md): HPA vs KEDA vs Karpenter in depth.
- [`evals/reports/benchmark.md`](../evals/reports/benchmark.md): the canonical benchmark table (FastAPI rows are real; GPU is documented).
