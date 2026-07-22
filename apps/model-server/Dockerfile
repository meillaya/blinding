# InferenceOps model-server image (todo 10): multi-stage, non-root.
#
# Build with the REPO ROOT as the build context so the builder can see the uv
# workspace metadata and the runtime can copy the trained artifact:
#
#   docker build -t inferenceops/model-server:dev -f apps/model-server/Dockerfile .
#
# (that is exactly what `make docker-build` runs).
#
#   builder  — install a pinned uv, then materialize the workspace .venv with
#              the runtime deps. The root pyproject depends on the three member
#              packages via [tool.uv.sources]; the members are `package = false`,
#              so `uv sync --frozen --no-dev` installs their dependency closure
#              without building the packages themselves (only the metadata +
#              uv.lock are needed here, not the source).
#   runtime  — copy the venv + the inferenceops_model_server package (run from
#              source) + the trained artifact, drop privileges, serve uvicorn.

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

# Non-root user. The server only reads the model and writes nothing persistent,
# so read-only access to /app (the default for COPY'd root-owned files) suffices.
RUN useradd --create-home --no-log-init --uid 1000 --user-group appuser

WORKDIR /app

# Bring in the materialized venv from the builder. Both stages use the same base
# image, so the venv's interpreter (home = /usr/local/bin) resolves identically.
COPY --from=builder /app/.venv /app/.venv

# The model-server package, run from source (it is `package = false`). Placing it
# at /app/apps/model-server/src/... makes LocalStorageBackend's parents[5] == /app,
# so its default artifact root is /app/models/artifacts (also pinned via MODELS_DIR).
COPY apps/model-server/src/ /app/apps/model-server/src/

# Trained artifact where LocalStorageBackend resolves it: <root>/<dir>/model.pkl +
# model_meta.json (resolution is metadata-driven by model_id/model_version).
COPY models/artifacts/fraud_detector/ /app/models/artifacts/fraud_detector/

ENV MODELS_DIR=/app/models/artifacts \
    PYTHONPATH=/app/apps/model-server/src \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8080

# python:3.12-slim ships no curl, so probe /healthz with the stdlib instead.
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz',timeout=2).status==200 else 1)"

CMD ["uvicorn", "inferenceops_model_server.server:app", "--host", "0.0.0.0", "--port", "8080"]
