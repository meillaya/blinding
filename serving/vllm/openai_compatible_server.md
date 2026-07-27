# vLLM — OpenAI-Compatible Server (GPU backend)

[vLLM](https://docs.vllm.ai/) serves Hugging Face LLMs behind an
OpenAI-compatible HTTP API. It is the **GPU backend option** for the platform's
`/v1/completions` + `/v1/chat/completions` routes (reserved by the gateway,
todo 11; wired to a backend in todo 43).

> **Status: build/doc-verified only.** There is no GPU on the development host,
> so the image (`serving/vllm/Dockerfile`) is lint-verified (hadolint) and the
> config/docs are validated — **runtime serving requires an NVIDIA GPU node and
> has NOT been exercised here.** Do not read this page as "serves a request".

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/models` | List served models (OpenAI-compatible) |
| `POST` | `/v1/completions` | Text completions (prompt → continuation) |
| `POST` | `/v1/chat/completions` | Chat completions (messages → reply) |
| `POST` | `/v1/embeddings` | Embeddings (requires an embedding model) |
| `GET` | `/health` | Readiness probe (200 once the model is loaded) |

## Prerequisites (GPU)

1. **NVIDIA GPU node group** — at least one node with an NVIDIA GPU (driver
   installed; e.g. a managed GPU node pool or a self-managed group labeled
   `inferenceops.ai/gpu=true`, matching `serving/vllm/values.yaml`).
2. **NVIDIA device plugin** — the
   [k8s-device-plugin](https://github.com/NVIDIA/k8s-device-plugin) DaemonSet,
   which advertises the `nvidia.com/gpu` extended resource and installs the
   `nvidia` container runtime class. Without it, a pod requesting
   `nvidia.com/gpu: 1` is unschedulable.
3. **Shared memory** — run containers with `--ipc=host` (Docker) or a large
   `/dev/shm` emptyDir (Kubernetes). torch/NCCL use POSIX shared memory; the
   default 64 MiB Docker `/dev/shm` crashes the server under load.

## Run (Docker, single GPU)

```bash
docker run --rm -d --name vllm \
  --gpus all --ipc=host \
  -p 8000:8000 \
  -v vllm-hf-cache:/root/.cache \
  inferenceops/vllm:dev
```

The image's `ENTRYPOINT ["vllm", "serve"]` (inherited from
`vllm/vllm-openai:v0.25.1`) + its default `CMD` compose to:

```bash
vllm serve Qwen/Qwen2.5-1.5B-Instruct --host 0.0.0.0 --port 8000 \
  --dtype auto --max-model-len 4096 --gpu-memory-utilization 0.90 \
  --tensor-parallel-size 1
```

Override the model or flags by passing arguments (they REPLACE the default
CMD and are appended to `vllm serve`):

```bash
docker run --rm --gpus all --ipc=host -p 8000:8000 \
  inferenceops/vllm:dev meta-llama/Llama-3.2-1B-Instruct --max-model-len 2048
```

Legacy form (equivalent, still supported in v0.25.x):

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-1.5B-Instruct --host 0.0.0.0 --port 8000 \
  --dtype auto --max-model-len 4096 --gpu-memory-utilization 0.90 \
  --tensor-parallel-size 1
```

## Run (Kubernetes, Helm)

```bash
helm upgrade --install vllm examples/deployment/chart-helm \
  -n inferenceops -f serving/vllm/values.yaml
```

`serving/vllm/values.yaml` pins the same image/model/flags and requests
`nvidia.com/gpu: 1` (limits + requests), the `nvidia` runtime class, the GPU
node selector, and an 8 Gi `/dev/shm` (the in-cluster `--ipc=host` equivalent).

## Try it (curl)

List models:

```bash
curl http://localhost:8000/v1/models
```

```json
{
  "object": "list",
  "data": [
    { "id": "Qwen/Qwen2.5-1.5B-Instruct", "object": "model", "owned_by": "vllm" }
  ]
}
```

Text completion:

```bash
curl http://localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-1.5B-Instruct","prompt":"The capital of France is","max_tokens":50}'
```

Chat completion:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-1.5B-Instruct","messages":[{"role":"user","content":"Say hello"}],"max_tokens":50}'
```

The server needs no API key by default; add `--api-key <key>` to the serve
arguments and `-H 'Authorization: Bearer <key>'` to the curls if you enable one.

## CPU fallback (basic)

For hosts without a GPU, vLLM has a **basic** CPU backend — functional for
smoke tests, NOT for production throughput:

```bash
docker run --rm -d --name vllm-cpu \
  -e VLLM_TARGET_DEVICE=cpu \
  -p 8000:8000 \
  vllm/vllm-openai-cpu:v0.25.1 \
  Qwen/Qwen2.5-1.5B-Instruct --dtype float32 --max-model-len 2048
```

- `VLLM_TARGET_DEVICE=cpu` selects the CPU code path.
- Use the dedicated `vllm/vllm-openai-cpu` image (the GPU image will not run
  without CUDA).
- Drop `--gpu-memory-utilization` / `--tensor-parallel-size` (GPU-only flags);
  `--dtype float32` is the safe CPU choice.

For a production-grade CPU LLM behind the gateway, prefer the platform's
FastAPI model-server path (todo 43) over vLLM-CPU.

## References

- Image: [`vllm/vllm-openai:v0.25.1`](https://hub.docker.com/r/vllm/vllm-openai)
  (GPU), [`vllm/vllm-openai-cpu:v0.25.1`](https://hub.docker.com/r/vllm/vllm-openai-cpu) (CPU)
- vLLM OpenAI-compatible server docs: <https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html>
- In-repo artifacts: `serving/vllm/Dockerfile`, `serving/vllm/values.yaml`,
  chart `examples/deployment/chart-helm` (chart-vllm)
