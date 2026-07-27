#!/usr/bin/env bash
# run_llm_cpu.sh — lifecycle manager for a SMALL CPU-only OpenAI-compatible LLM
# (todo 43). Starts either llama.cpp's `llama-server` (default) or `ollama
# serve`, waits for the OpenAI surface (/v1/models) to become ready with a
# BOUNDED timeout, and tears the server down cleanly. The gateway proxies
# /v1/completions + /v1/chat/completions + /v1/models to whichever backend this
# starts (see serving/llm-cpu/README.md for the wiring).
#
# CPU-only by design: no GPU on the host, so the model is tiny (Qwen2.5-0.5B
# Q4_0 GGUF, ~410 MB, or ollama's qwen2.5:0.5b). The download is bounded
# (DOWNLOAD_TIMEOUT_S) and the readiness wait is bounded (READY_TIMEOUT_S) so a
# stuck pull or a failed load can never hang the smoke.
#
# Usage:
#   scripts/run_llm_cpu.sh start    # download (if needed) + start + wait ready
#   scripts/run_llm_cpu.sh stop     # kill the server this script started
#   scripts/run_llm_cpu.sh status   # 0 if ready, 1 otherwise
#
# Knobs (env):
#   LLM_BACKEND          llama-cpu (default) | ollama
#   LLM_HOST             bind address (default 0.0.0.0 — reachable from kind)
#   LLM_PORT             8001 (llama-cpu) | 11434 (ollama) by default
#   MODEL_URL/MODEL_FILE GGUF source / local path (llama-cpu)
#   OLLAMA_MODEL         default qwen2.5:0.5b
#   READY_TIMEOUT_S      default 120   DOWNLOAD_TIMEOUT_S  default 600
set -euo pipefail

LLM_BACKEND="${LLM_BACKEND:-llama-cpu}"
LLM_HOST="${LLM_HOST:-0.0.0.0}"
case "$LLM_BACKEND" in
  llama-cpu) DEFAULT_PORT=8001 ;;
  ollama) DEFAULT_PORT=11434 ;;
  *)
    echo "ERROR: unknown LLM_BACKEND '$LLM_BACKEND' (want llama-cpu|ollama)" >&2
    exit 2
    ;;
esac
LLM_PORT="${LLM_PORT:-$DEFAULT_PORT}"

MODEL_CACHE="${MODEL_CACHE:-$HOME/.cache/inferenceops/llm-cpu}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_0.gguf}"
MODEL_FILE="${MODEL_FILE:-$MODEL_CACHE/qwen2.5-0.5b-instruct-q4_0.gguf}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:0.5b}"

READY_TIMEOUT_S="${READY_TIMEOUT_S:-120}"
DOWNLOAD_TIMEOUT_S="${DOWNLOAD_TIMEOUT_S:-600}"

RUNTIME_DIR="${RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/inferenceops-llm-cpu}"
PID_FILE="$RUNTIME_DIR/$LLM_BACKEND.pid"
LOG_FILE="$RUNTIME_DIR/$LLM_BACKEND.log"

base_url() {
  # The loopback URL a same-host caller (or the smoke) uses to reach the server.
  echo "http://127.0.0.1:$LLM_PORT"
}

is_ready() {
  # The OpenAI model list is the readiness signal: it answers 200 only once the
  # backend is up AND (for llama-server) the model is loaded.
  curl -sf --max-time 3 "$(base_url)/v1/models" >/dev/null 2>&1
}

pid_alive() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

download_model() {
  if [ -s "$MODEL_FILE" ]; then
    echo "model already present: $MODEL_FILE"
    return 0
  fi
  mkdir -p "$MODEL_CACHE"
  echo "downloading model (bounded ${DOWNLOAD_TIMEOUT_S}s): $MODEL_URL"
  local part="$MODEL_FILE.part"
  if ! curl -fL --max-time "$DOWNLOAD_TIMEOUT_S" -o "$part" "$MODEL_URL"; then
    rm -f "$part"
    echo "ERROR: model download failed or timed out after ${DOWNLOAD_TIMEOUT_S}s" >&2
    return 1
  fi
  mv "$part" "$MODEL_FILE"
  echo "model ready: $MODEL_FILE"
}

start_llama_cpu() {
  command -v llama-server >/dev/null 2>&1 || {
    echo "ERROR: llama-server not on PATH (install llama.cpp)" >&2
    return 1
  }
  download_model
  setsid llama-server \
    --model "$MODEL_FILE" \
    --alias small-model \
    --host "$LLM_HOST" \
    --port "$LLM_PORT" \
    --ctx-size 2048 \
    --threads "$(nproc)" \
    >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  echo "llama-server started (pid $(cat "$PID_FILE"), log $LOG_FILE)"
}

start_ollama() {
  command -v ollama >/dev/null 2>&1 || {
    echo "ERROR: ollama not on PATH" >&2
    return 1
  }
  export OLLAMA_HOST="$LLM_HOST:$LLM_PORT"
  if is_ready; then
    echo "ollama already serving on $(base_url); reusing it"
  else
    setsid ollama serve >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    echo "ollama serve started (pid $(cat "$PID_FILE"), log $LOG_FILE)"
    for _ in $(seq 1 30); do
      is_ready && break
      sleep 1
    done
  fi
  echo "pulling model (bounded ${DOWNLOAD_TIMEOUT_S}s): $OLLAMA_MODEL"
  if ! timeout "$DOWNLOAD_TIMEOUT_S" ollama pull "$OLLAMA_MODEL"; then
    echo "ERROR: ollama pull failed or timed out after ${DOWNLOAD_TIMEOUT_S}s" >&2
    return 1
  fi
}

wait_ready() {
  echo "waiting up to ${READY_TIMEOUT_S}s for $(base_url)/v1/models ..."
  local deadline=$((SECONDS + READY_TIMEOUT_S))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if is_ready; then
      echo "READY: $(base_url) serves the OpenAI surface"
      return 0
    fi
    if ! pid_alive && [ "$LLM_BACKEND" = "llama-cpu" ]; then
      echo "ERROR: llama-server exited before becoming ready; last log lines:" >&2
      tail -n 20 "$LOG_FILE" >&2 || true
      return 1
    fi
    sleep 1
  done
  echo "ERROR: LLM backend not ready after ${READY_TIMEOUT_S}s (see $LOG_FILE)" >&2
  return 1
}

cmd_start() {
  mkdir -p "$RUNTIME_DIR"
  if pid_alive && is_ready; then
    echo "already running + ready: $(base_url)"
    return 0
  fi
  case "$LLM_BACKEND" in
    llama-cpu) start_llama_cpu ;;
    ollama) start_ollama ;;
  esac
  wait_ready
}

cmd_stop() {
  local rc=0
  if pid_alive; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "stopping $LLM_BACKEND (pid $pid)"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 15); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
  # Char-class backstop: kill any stray server bound to our port (the [...]
  # keeps this pkill from matching its own command line).
  case "$LLM_BACKEND" in
    llama-cpu) pkill -f "llama-serve[r].*--port $LLM_PORT" 2>/dev/null || true ;;
    ollama) pkill -f "ollam[a] serve" 2>/dev/null || true ;;
  esac
  rm -f "$PID_FILE"
  if curl -sf --max-time 2 "$(base_url)/v1/models" >/dev/null 2>&1; then
    echo "WARNING: something still answers on $(base_url) after teardown" >&2
    rc=1
  else
    echo "teardown clean: nothing on $(base_url)"
  fi
  return "$rc"
}

cmd_status() {
  if is_ready; then
    echo "READY: $(base_url)"
    return 0
  fi
  echo "NOT READY: $(base_url)"
  return 1
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
