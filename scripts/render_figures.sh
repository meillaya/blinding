#!/usr/bin/env bash
# Render the committed Excalidraw scenes. The browser profile and the CLI
# caches are pointed at the workspace so nothing lands in the home directory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HOME="$ROOT/.sandbox/home"
export TMPDIR="$ROOT/.sandbox/tmp"
export XDG_CACHE_HOME="$ROOT/.sandbox/cache-host"
export XDG_CONFIG_HOME="$ROOT/.sandbox/config"
mkdir -p "$HOME" "$TMPDIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME"

for name in architecture bench-methodology tuning-frontier; do
    excalidraw render "$ROOT/docs/figures/$name.excalidraw" \
        --out "$ROOT/docs/figures/$name.png" \
        --scale 2 --max-width 3200 --padding 20
done
