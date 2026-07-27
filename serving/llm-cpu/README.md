# CPU LLM — OpenAI-Compatible Backend (llama.cpp / ollama)

The **CPU-runnable LLM path** for the platform's OpenAI-compatible gateway
routes (`/v1/completions`, `/v1/chat/completions`, `/v1/models`). Where
[`serving/vllm/`](../vllm/) is the GPU backend, this is the **no-GPU** option:
a SMALL model served on the host CPU by either [llama.cpp]'s `llama-server`
(default) or [ollama], both of which expose the OpenAI HTTP API out of the box.

> **Status: implemented + smoke-verified on CPU.** `make llm-cpu-smoke` starts
> the host LLM, points the in-cluster gateway at it, and drives a real
> completion through the gateway ingress — all on CPU. The model is tiny
> (Qwen2.5-0.5B, ~410 MB) so it downloads and loads within a bounded budget.

[llama.cpp]: https://github.com/ggml-org/llama.cpp
[ollama]: https://ollama.com

## Endpoints (OpenAI-compatible)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/models` | List served models |
| `POST` | `/v1/completions` | Text completions (prompt → continuation) |
| `POST` | `/v1/chat/completions` | Chat completions (messages → reply) |

llama.cpp also exposes `GET /health` (readiness: `ok` once the model loads).

## Backends (`LLM_BACKEND`)

The gateway selects the backend by the `LLM_BACKEND` env var; each maps to a
default base URL (`inferenceops_gateway.config.LLM_BACKEND_DEFAULT_URLS`),
overridable with `LLM_URL`:

| `LLM_BACKEND` | Server | Default URL | Model |
| --- | --- | --- | --- |
| `llama-cpu` (default) | `llama-server` (llama.cpp) | `http://localhost:8001` | Qwen2.5-0.5B-Instruct Q4_0 GGUF (~410 MB) |
| `ollama` | `ollama serve` | `http://localhost:11434` | `qwen2.5:0.5b` (~350 MB) |
| `vllm` | vLLM OpenAI server | `http://localhost:8000` | see [`serving/vllm/`](../vllm/) (GPU) |

## Run the CPU LLM

[`scripts/run_llm_cpu.sh`](../../scripts/run_llm_cpu.sh) manages the full
lifecycle — bounded model download, start, bounded readiness wait, clean
teardown:

```bash
scripts/run_llm_cpu.sh start     # download (if needed) + start + wait for /v1/models
scripts/run_llm_cpu.sh status    # 0 when ready
scripts/run_llm_cpu.sh stop      # kill the server + verify the port is free

# ollama instead of llama.cpp:
LLM_BACKEND=ollama scripts/run_llm_cpu.sh start
```

The server binds `0.0.0.0` so it is reachable from the kind cluster (below).
State (pidfile + log) lives under `${XDG_RUNTIME_DIR:-/tmp}/inferenceops-llm-cpu/`;
the model cache under `~/.cache/inferenceops/llm-cpu/`.

### Direct curl (host)

```bash
curl http://localhost:8001/v1/models

curl http://localhost:8001/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"small-model","prompt":"Hello","max_tokens":16}'

curl http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"small-model","messages":[{"role":"user","content":"Hi"}],"max_tokens":16}'
```

(`model` is `small-model` — the `--alias` llama-server is started with; for
ollama use the pulled tag, e.g. `qwen2.5:0.5b`.)

## Gateway wiring

The gateway proxies the OpenAI surface to the configured backend
(`apps/gateway/src/inferenceops_gateway/routes/llm.py`). Auth (`X-API-Key`) and
rate-limiting apply to these routes exactly as on `/v1/predict`. With **no**
`LLM_BACKEND` configured the routes answer **503** and `/v1/models` stays on the
model-server proxy; with a backend configured `/v1/models` serves the LLM's
OpenAI model list.

```bash
# Point the deployed gateway at a host LLM (LLM_URL overrides the backend default):
kubectl -n inferenceops set env deployment/gateway \
  LLM_BACKEND=llama-cpu LLM_URL=http://<host-ip>:8001
kubectl -n inferenceops rollout status deployment/gateway

# Through the ingress (Host header + API key):
curl http://localhost:8080/v1/completions \
  -H 'Host: inferenceops.local' \
  -H 'X-API-Key: dev-key-REPLACE-ME-0000000000000000' \
  -H 'Content-Type: application/json' \
  -d '{"model":"small-model","prompt":"Hello","max_tokens":16}'
```

### Networking: gateway pod → host LLM (rootless kind)

The gateway runs **in** the kind cluster; the CPU LLM runs **on the host**. On
this rootless-docker + slirp4netns host, a pod reaches the host via the host's
**LAN IP** (slirp4netns forwards to the host network) — **not** the kind bridge
gateway (`172.18.0.1`, which refuses) and not `host.docker.internal` (does not
resolve). Discover it on the host:

```bash
ip route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}'
```

`scripts/llm_cpu_smoke.sh` derives this automatically and verifies pod→host
reachability before configuring the gateway.

## Smoke test

```bash
make llm-cpu-smoke
```

Builds + loads the gateway image, starts the host LLM, points the gateway at it,
asserts a non-empty `/v1/completions` + a populated `/v1/models` through the
ingress, then restores the gateway env and stops the LLM. Transcript:
`/tmp/qa/t43_llm.txt`. `SKIP_BUILD=1` reuses an already-loaded gateway image.
