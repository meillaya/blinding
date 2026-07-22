# InferenceOps gateway image (todo 12): multi-stage, non-root.
#
# Build with the REPO ROOT as the build context so the builder can see the uv
# workspace metadata (the gateway is a `package = false` workspace member run
# from source, exactly like the model-server image from todo 10):
#
#   docker build -t inferenceops/gateway:dev -f apps/gateway/Dockerfile .
#
#   builder  — install a pinned uv, then materialize the workspace .venv with
#              the runtime deps. The root pyproject depends on the three member
#              packages via [tool.uv.sources]; the members are `package = false`,
#              so `uv sync --frozen --no-dev` installs their dependency closure
#              without building the packages themselves (only the metadata +
#              uv.lock are needed here, not the source).
#   runtime  — copy the venv + the inferenceops_gateway package (run from
#              source), drop privileges, serve uvicorn. The gateway proxies to
#              the model server at MODEL_SERVER_URL (set by the caller/compose);
#              it ships no model artifact of its own.

# ---- builder -------------------------------------------------------------
FROM python:3.12-slim AS builder

# Pinned uv (matches the workspace toolchain, uv 0.11.30): static binary copied
# from the official image — no pip layer, reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /bin/

WORKDIR /app

# Copy ONLY the uv-workspace metadata first. This is the cache-friendly layer:
# dependencies are re-synced only when the lockfile/metadata change, not on every
# source edit. The three member pyprojects are required so uv can resolve the
# workspace; their source trees are not (package = false).
COPY pyproject.toml uv.lock ./
COPY apps/gateway/pyproject.toml apps/gateway/pyproject.toml
COPY apps/model-server/pyproject.toml apps/model-server/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml

# Materialize the runtime venv at /app/.venv (no dev group).
#   UV_LINK_MODE=copy      — copy files instead of hardlinking (overlayfs-safe).
#   UV_COMPILE_BYTECODE=1  — precompile the venv's bytecode for faster imports.
#   UV_PYTHON_DOWNLOADS=never — use the base image's CPython, never fetch one.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-dev

# ---- runtime -------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root user. The gateway only proxies requests and writes nothing persistent,
# so read-only access to /app (the default for COPY'd root-owned files) suffices.
RUN useradd --create-home --no-log-init --uid 1000 --user-group appuser

WORKDIR /app

# Bring in the materialized venv from the builder. Both stages use the same base
# image, so the venv's interpreter (home = /usr/local/bin) resolves identically.
COPY --from=builder /app/.venv /app/.venv

# The gateway package, run from source (it is `package = false`).
COPY apps/gateway/src/ /app/apps/gateway/src/

ENV PYTHONPATH=/app/apps/gateway/src \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8080

# python:3.12-slim ships no curl, so probe /healthz with the stdlib instead.
# /healthz is exempt from API-key auth (auth.py), so the probe needs no key.
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz',timeout=2).status==200 else 1)"

CMD ["uvicorn", "inferenceops_gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
