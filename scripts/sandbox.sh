#!/usr/bin/env bash
# Workspace-local podman sandbox.
#
# Every file the engine or the container writes stays under .sandbox/ inside
# this checkout. The container root is read-only; the checkout is the only bind
# mount and /tmp is a tmpfs. GPU work passes only /dev/kfd and the render node.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$ROOT/.sandbox"
IMAGE_CPU="localhost/blinding-cpu:dev"
IMAGE_ROCM="localhost/blinding-rocm:dev"

export HOME="$SANDBOX/home"
export XDG_RUNTIME_DIR="$SANDBOX/run"
export TMPDIR="$SANDBOX/tmp"
export XDG_CONFIG_HOME="$SANDBOX/config"
export XDG_CACHE_HOME="$SANDBOX/cache-host"
export XDG_DATA_HOME="$SANDBOX/data"

mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$TMPDIR" "$XDG_CONFIG_HOME" \
    "$XDG_CACHE_HOME" "$XDG_DATA_HOME" \
    "$SANDBOX/cache" "$SANDBOX/uv-cache" "$SANDBOX/pip-cache" \
    "$SANDBOX/venv-cpu" "$SANDBOX/venv-rocm" "$SANDBOX/ruff-cache" "$SANDBOX/xdg-cache"

if ! command -v podman >/dev/null 2>&1; then
    echo "sandbox: podman is not on PATH" >&2
    exit 4
fi

ensure_config() {
    mkdir -p "$HOME/.config/containers"
    cp -f "$ROOT/sandbox/containers/policy.json" "$HOME/.config/containers/policy.json"
    cp -f "$ROOT/sandbox/containers/registries.conf" "$HOME/.config/containers/registries.conf"
}

PODMAN=(podman --root "$SANDBOX/containers" --runroot "$SANDBOX/run")
if [[ -f "$SANDBOX/storage-driver" ]]; then
    PODMAN+=(--storage-driver "$(cat "$SANDBOX/storage-driver")")
fi

usage() {
    cat <<'EOF'
usage: scripts/sandbox.sh <command> [options]

  init                 create the workspace podman store
  build --cpu|--rocm   build an image
  run   --cpu|--rocm   run a command in a fresh container
  shell --cpu|--rocm   open a shell in a fresh container
  gpu-check            run rocminfo, rocm-smi, and the HIP benchmark
  verify               check the read-only and workspace-only guarantees
  clean                remove the store, images, containers, and caches
EOF
}

container_env() {
    local variant="${1:-cpu}"
    printf '%s\n' \
        --env "HOME=/tmp" \
        --env "TMPDIR=/tmp" \
        --env "BLINDING_ROOT=$ROOT" \
        --env "BLINDING_CACHE=$SANDBOX/cache" \
        --env "UV_PROJECT_ENVIRONMENT=$SANDBOX/venv-$variant" \
        --env "UV_CACHE_DIR=$SANDBOX/uv-cache" \
        --env "PIP_CACHE_DIR=$SANDBOX/pip-cache" \
        --env "RUFF_CACHE_DIR=$SANDBOX/ruff-cache" \
        --env "XDG_CACHE_HOME=$SANDBOX/xdg-cache" \
        --env "PYTHONDONTWRITEBYTECODE=1" \
        --env "PYTEST_ADDOPTS=-p no:cacheprovider"
}

run_container() {
    local image="$1"
    local gpu="$2"
    shift 2
    local variant="cpu"
    [[ "$image" == *rocm* ]] && variant="rocm"
    local args=(
        run --rm
        --read-only
        --network=host
        --volume "$ROOT:$ROOT"
        --workdir "$ROOT"
        --tmpfs "/tmp:rw,size=8g"
        --tmpfs "/run:rw,size=1g"
        --tmpfs "/dev/shm:rw,size=4g"
    )
    while IFS= read -r item; do
        args+=("$item")
    done < <(container_env "$variant")
    if [[ "$gpu" == "1" ]]; then
        args+=(--device /dev/kfd --device /dev/dri/renderD128 --group-add keep-groups)
    fi
    "${PODMAN[@]}" "${args[@]}" "$image" "$@"
}

cmd_init() {
    ensure_config
    "${PODMAN[@]}" info >/dev/null
    local driver
    driver="$("${PODMAN[@]}" info --format '{{.Store.GraphDriverName}}')"
    printf '%s\n' "$driver" >"$SANDBOX/storage-driver"
    echo "store    $SANDBOX/containers"
    echo "driver   $driver"
    echo "kfd      $([[ -e /dev/kfd ]] && echo present || echo absent)"
    echo "render   $([[ -e /dev/dri/renderD128 ]] && echo present || echo absent)"
}

cmd_build() {
    local mode="${1:-}"
    local image file
    case "$mode" in
        --cpu) image="$IMAGE_CPU"; file="Containerfile.cpu" ;;
        --rocm) image="$IMAGE_ROCM"; file="Containerfile.rocm" ;;
        *) echo "build needs --cpu or --rocm" >&2; exit 2 ;;
    esac
    ensure_config
    "${PODMAN[@]}" build --network=host --tag "$image" \
        --file "$ROOT/sandbox/$file" "$ROOT"
}

cmd_run() {
    local mode="--cpu"
    if [[ "${1:-}" == "--cpu" || "${1:-}" == "--rocm" ]]; then
        mode="$1"
        shift
    fi
    [[ $# -gt 0 ]] || {
        echo "run needs a command" >&2
        exit 2
    }
    if [[ "$mode" == "--rocm" ]]; then
        run_container "$IMAGE_ROCM" 1 "$@"
    else
        run_container "$IMAGE_CPU" 0 "$@"
    fi
}

cmd_shell() {
    local mode="${1:---cpu}"
    if [[ "$mode" == "--rocm" ]]; then
        run_container "$IMAGE_ROCM" 1 bash -l
    else
        run_container "$IMAGE_CPU" 0 bash -l
    fi
}

cmd_gpu_check() {
    run_container "$IMAGE_ROCM" 1 bash -lc '
        set -e
        rocminfo | grep -m1 -E "gfx|Marketing" || true
        rocm-smi --showproductname --showmeminfo vram || true
        blinding-hip-bench --blocks 128 --sizes 1024 --dtypes fp32 --layouts nn
    '
}

cmd_verify() {
    ensure_config
    local root
    root="$("${PODMAN[@]}" info --format '{{.Store.GraphRoot}}')"
    case "$root" in
        "$SANDBOX"/*) echo "store    inside workspace" ;;
        *) echo "store    OUTSIDE workspace: $root" >&2; exit 3 ;;
    esac
    run_container "$IMAGE_CPU" 0 bash -lc '
        set -e
        if touch /root/forbidden 2>/dev/null; then
            echo "readonly root writable" >&2
            exit 3
        fi
        test -w /work || { echo "workspace not writable" >&2; exit 3; }
        echo "readonly root denied writes"
        echo "workspace writable"
    '
}

cmd_clean() {
    ensure_config || true
    "${PODMAN[@]}" ps -aq 2>/dev/null | while read -r id; do
        [[ -n "$id" ]] && "${PODMAN[@]}" rm -f "$id" >/dev/null 2>&1 || true
    done
    "${PODMAN[@]}" images -q 2>/dev/null | while read -r id; do
        [[ -n "$id" ]] && "${PODMAN[@]}" rmi -f "$id" >/dev/null 2>&1 || true
    done
    chmod -R u+rwX "$SANDBOX" 2>/dev/null || true
    rm -rf "$SANDBOX"
    echo "removed $SANDBOX"
}

command="${1:-}"
shift || true
case "$command" in
    init) cmd_init ;;
    build) cmd_build "$@" ;;
    run) cmd_run "$@" ;;
    shell) cmd_shell "$@" ;;
    gpu-check) cmd_gpu_check ;;
    verify) cmd_verify ;;
    clean) cmd_clean ;;
    *) usage; exit 2 ;;
esac
