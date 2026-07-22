# fraud_detector_canary — candidate model artifact (todo 29)

This is the **candidate** artifact the nginx canary demo routes ~10% of traffic
to. It exists so the canary `model-server` reports a DIFFERENT `model_version`
(`2.0.0`) than the stable tier (`1.0.0`) — the `model_version` metric/response
label is the discriminator the canary split and the automated rollback (todo 30)
key off.

- `model_meta.json` — `model_id: fraud-detector`, **`model_version: 2.0.0`**.
  Resolution by `LocalStorageBackend` is metadata-driven (it matches the
  `model_id`/`model_version` recorded HERE, not the directory name), so this dir
  is what `get_artifact("fraud-detector", "2.0.0")` resolves to.
- `model.pkl` — a byte-for-byte copy of `../fraud_detector/model.pkl` (same
  trained `GradientBoostingClassifier`). For the routing demo the weights are
  intentionally identical; only the version label differs. A real canary would
  carry a genuinely retrained artifact here.
- `metrics.json` — copied from the stable artifact (same model).

`scripts/deploy_canary.sh` bakes this directory into the `inferenceops/model-server:dev`
image (see `serving/fastapi/deployment.md`) so a canary pod started with
`MODEL_VERSION=2.0.0` can load it and become Ready.
