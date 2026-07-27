"""Ray Serve scaffold for the InferenceOps fraud-detection model-server.

================================================================================
 SCAFFOLD ONLY. NO RUNNING RAY IMPLEMENTATION.
================================================================================
 This file is a DOCUMENTED EXAMPLE of how the InferenceOps model-server
 (apps/model-server) WOULD be served behind Ray Serve. It is deliberately NOT:

   * wired into CI. No workflow under .github/workflows/ runs it.
   * wired into docker-compose. There is no `ray start` anywhere in this repo.
   * a project dependency. `ray` is NOT in pyproject.toml and is never installed.
   * exercised by any test, load run, or deploy script.

 The production serving path is the FastAPI model-server (apps/model-server,
 deployed by k8s/base/deployment-model-server.yaml). Ray Serve is documented
 here only as one of the four backends compared in serving/README.md.

 Autoscaling story (queue-size monitoring): Ray Serve's autoscaler counts the
 ONGOING REQUESTS queued at each replica (its "queue size") and adds or removes
 replicas to hold the per-replica average at `target_ongoing_requests`. That is
 the direct analogue of the FastAPI server's external-RPS HPA metric
 (inferenceops_rps in k8s/overlays/local/patch-hpa-custom-rps.yaml). Both scale
 on request load, not CPU. See docs/autoscaling-strategy.md.

 Verification: `python -m py_compile serving/ray-serve/deployment.py` exits 0.
 py_compile only checks syntax; it never imports, so this compiles WITHOUT ray
 installed. Running the file directly (`python deployment.py`) starts nothing:
 it prints this banner and exits 0. Importing it does require `ray[serve]`
 (absent here by design), so even a stray run cannot start a Ray cluster.
================================================================================
"""

from __future__ import annotations

# The InferenceOps predictor this scaffold wraps. Importable when
# PYTHONPATH=apps/model-server/src (the repo convention; see the Makefile
# `run-local` target). py_compile does not resolve these imports either.
from inferenceops_model_server.model_loader import load_model
from inferenceops_model_server.predictor import Prediction, predict
from inferenceops_model_server.storage.local import LocalStorageBackend

# --- Ray Serve imports (NOT installed in this repo; scaffold only) -----------
# py_compile does not import these, so the file compiles without ray present.
# A real deployment would first `pip install "ray[serve]"`.
from ray import serve
from ray.serve import AutoscalingConfig

# The fraud model the FastAPI server ships with (models/artifacts/fraud_detector).
_MODEL_ID = "fraud-detector"
_MODEL_VERSION = "1.0.0"


@serve.deployment(
    # --- Queue-size autoscaling (the Ray Serve autoscaling story) ------------
    # The autoscaler watches ongoing (queued) requests per replica and scales
    # to hold the average at `target_ongoing_requests`. Mirrors the HPA's
    # external-RPS target (averageValue "30" per replica) and its replica band
    # (minReplicas 2 / maxReplicas 10) in k8s/base/hpa.yaml.
    autoscaling_config=AutoscalingConfig(
        min_replicas=2,  # HA floor (matches HPA minReplicas: 2)
        max_replicas=10,  # ceiling (matches HPA maxReplicas: 10)
        initial_replicas=2,
        target_ongoing_requests=30,  # ~30 in-flight reqs per replica
        smoothing_factor=1.0,
        upscale_delay_s=10,
        downscale_delay_s=60,  # mirror the HPA's 60s scale-down stabilization
    ),
    ray_actor_options={
        "num_cpus": 2,  # matches the model-server requests.cpu "2"
        # "num_gpus": 1,  # UNCOMMENT for a GPU model (e.g. vLLM). The default
        # fraud model is CPU-only, so no GPU is requested.
    },
)
class FraudDetectorDeployment:
    """One Ray Serve replica: a loaded fraud model plus a predict handler.

    Each replica loads the artifact once at startup, exactly like the FastAPI
    server's lifespan (apps/model-server/src/inferenceops_model_server/
    server.py). Storage resolves from the MODELS_DIR env var or the repo's
    models/artifacts default, mirroring model_loader.py.
    """

    def __init__(self, model_id: str, version: str) -> None:
        self._model = load_model(LocalStorageBackend(), model_id, version)

    async def __call__(self, features: list[float]) -> Prediction:
        # Ray Serve routes each request to a replica and counts it as an
        # ongoing request for the autoscaler's queue-size math above.
        return predict(self._model, features)


# Module-level application graph. `serve deploy serve_config.yaml` imports this
# via `import_path: deployment:app` and serves it at the configured route_prefix.
# `.bind()` is lazy: it builds the DAG node without starting anything.
app = FraudDetectorDeployment.bind(model_id=_MODEL_ID, version=_MODEL_VERSION)


if __name__ == "__main__":
    # SCAFFOLD ONLY: this entrypoint deliberately starts NOTHING. It prints the
    # banner and exits 0, so a stray `python deployment.py` is a no-op rather
    # than a running Ray cluster. A real deploy would use `serve run
    # deployment:app` or `serve deploy serve_config.yaml`; neither is wired
    # into this repo. (Reaching this line at all requires ray to be installed,
    # which it is not here, so the import above fails first by design.)
    import sys

    sys.stdout.write(__doc__ or "")
    raise SystemExit(0)
