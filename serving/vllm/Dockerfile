# InferenceOps vLLM GPU serving image (todo 42): OpenAI-compatible LLM server.
#
# Thin derivation of the official vLLM OpenAI image — the base already ships the
# full vLLM stack (CUDA, torch, the `vllm` CLI) and declares
# `ENTRYPOINT ["vllm", "serve"]`, so the CMD below supplies only the default
# ARGUMENTS. The composed command is exactly:
#
#   vllm serve Qwen/Qwen2.5-1.5B-Instruct --host 0.0.0.0 --port 8000 \
#     --dtype auto --max-model-len 4096 --gpu-memory-utilization 0.90 \
#     --tensor-parallel-size 1
#
# (the legacy form `python -m vllm.entrypoints.openai.api_server --model …`
# is equivalent and documented in openai_compatible_server.md).
#
# Override the model or any flag at runtime — `docker run` arguments REPLACE
# this CMD and are appended to the inherited `vllm serve` entrypoint:
#
#   docker run --rm --gpus all --ipc=host -p 8000:8000 \
#     inferenceops/vllm:dev meta-llama/Llama-3.2-1B-Instruct --max-model-len 2048
#
# GPU REQUIRED: this image needs an NVIDIA GPU + the NVIDIA container runtime
# (`--gpus all`). It will NOT serve on a CPU-only host — see the CPU fallback
# (VLLM_TARGET_DEVICE=cpu + vllm/vllm-openai-cpu) in openai_compatible_server.md.
#
# --ipc=host (or --shm-size=8g) is REQUIRED: torch/NCCL use /dev/shm shared
# memory; the default 64 MiB Docker shm crashes the server under load.
#
# Non-root: the published vllm/vllm-openai image runs as root by default and
# needs the NVIDIA device nodes; we intentionally do NOT add a USER here. In
# Kubernetes the NVIDIA device plugin + runtime class handle device access (see
# values.yaml / openai_compatible_server.md).

# Pinned release tag (never `latest` — reproducible, hadolint DL3007).
FROM vllm/vllm-openai:v0.25.1

# The OpenAI-compatible API port (vLLM default). Model weights are fetched on
# first start (Hugging Face Hub) unless a cache volume is mounted at /root/.cache.
EXPOSE 8000

# vLLM exposes GET /health once the model is loaded. Model download + load can
# take minutes on first start, so allow a long start period. python3 is present
# in the base image (curl is not guaranteed), so probe with the stdlib.
HEALTHCHECK --interval=15s --timeout=5s --start-period=600s --retries=12 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=4).status==200 else 1)"

# Default arguments to the inherited `ENTRYPOINT ["vllm", "serve"]`.
#   --dtype auto                 — pick fp16/bf16 from the model config.
#   --max-model-len 4096         — cap context length (bounds KV-cache VRAM).
#   --gpu-memory-utilization 0.90 — headroom for CUDA context + activations.
#   --tensor-parallel-size 1     — single GPU (raise with one GPU per unit).
CMD ["Qwen/Qwen2.5-1.5B-Instruct", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--dtype", "auto", \
     "--max-model-len", "4096", \
     "--gpu-memory-utilization", "0.90", \
     "--tensor-parallel-size", "1"]
