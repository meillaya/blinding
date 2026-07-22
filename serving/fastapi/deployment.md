# Deployment & canary release (FastAPI model-server)

How the InferenceOps model-server is deployed and how a candidate model version
is rolled out behind an **nginx canary-weight split** (`make deploy-canary` /
`make promote` / `make rollback`, todo 29). The automated health-driven rollback
(todo 30) builds on this manual tooling.

## Request topology (Oracle F2)

**Base path** (steady state, todo 17):

```
client ──> ingress(inferenceops) ──> service-gateway ──> service-model-server
                  host: inferenceops.local      (auth + rate-limit)   (model_version=1.0.0)
```

The base ingress (`k8s/base/ingress.yaml`, object name `inferenceops`) fronts the
**gateway**, which authenticates (`X-API-Key`), rate-limits, and proxies to the
model-server.

**Canary path** (during the canary window): the canary `stable-ingress`
**replaces** the base ingress for host `inferenceops.local` — same object name
`inferenceops`, so `kubectl apply` overwrites it in place. The two ingresses do
**not** coexist (coexisting same-host ingresses with different backends is
undefined nginx behavior). The split happens at the **model-server tier**:

```
                                   ┌─(90%)─> service-model-server          (stable,    1.0.0)
client ──> nginx (host inferenceops.local)
                                   └─(10%)─> service-model-server-canary   (candidate, 2.0.0)
        stable-ingress  (inferenceops)         -> service-model-server
        canary-ingress  (inferenceops-canary)  -> service-model-server-canary
                            nginx.ingress.kubernetes.io/canary: "true"
                            nginx.ingress.kubernetes.io/canary-weight: "10"
```

The **gateway is bypassed** during the canary window so the 90/10 split happens
cleanly at the model-server tier. Consequence: **`/v1/predict` needs NO
`X-API-Key` while the canary is live** (auth/rate-limit live in the gateway).
`make rollback` / the base ingress reinstate the gateway, restoring auth.

The candidate vs stable traffic is discriminated by the **`model_version`
metric/response label** (todo 8): the canary pods run with `MODEL_VERSION=2.0.0`
and stamp it on every `/v1/predict` response body and on every `inferenceops_*`
metric, so the split is observable in the response and in Prometheus.

## The candidate artifact

The canary loads a **second model version** so its `model_version` label differs
from stable. `models/artifacts/fraud_detector_canary/` carries
`model_meta.json` with `model_id: fraud-detector`, `model_version: 2.0.0`
(plus a `model.pkl` — a copy of the stable model for the routing demo; a real
canary would carry a genuinely retrained artifact). Resolution by
`LocalStorageBackend` is **metadata-driven** (it matches the `model_id`/
`model_version` in `model_meta.json`, not the directory name), so this directory
is what `get_artifact("fraud-detector", "2.0.0")` resolves to. Model versioning
is decoupled from git semver.

Because the canary pod runs the **same image** as stable
(`inferenceops/model-server:dev`) and the committed Dockerfile only bakes in the
stable artifact, `scripts/deploy_canary.sh` **augments the image at deploy time**:
it builds `FROM` the current dev image (by immutable ID) with one extra
`COPY models/artifacts/fraud_detector_canary/ ...`, re-tags as
`inferenceops/model-server:dev`, and `kind load`s it. Both artifacts then resolve
from one image — stable pods (env `MODEL_VERSION=1.0.0`) load 1.0.0, canary pods
(env `MODEL_VERSION=2.0.0`) load 2.0.0. The committed
`apps/model-server/Dockerfile` is never modified.

## Manifests (`k8s/canary/`)

| File | Object | Role |
| --- | --- | --- |
| `canary-deployment.yaml` | Deployment `model-server-canary` + Service `service-model-server-canary` | Candidate tier; `MODEL_VERSION=2.0.0`, `imagePullPolicy: Never`, readiness `/readyz`, liveness `/healthz`. 1 replica (the split is by ingress **weight**, not replica count). |
| `stable-ingress.yaml` | Ingress `inferenceops` | Replaces the base ingress for the host; `-> service-model-server` (stable 90%). |
| `canary-ingress.yaml` | Ingress `inferenceops-canary` | Same host; `canary: "true"` + `canary-weight: "10"`; `-> service-model-server-canary` (candidate 10%). `ingressClassName: nginx`. |

Both ingresses share host `inferenceops.local` and `ingressClassName: nginx` —
required for the controller to merge the canary into the main server block and
split by weight.

## Lifecycle

```bash
# 90/10 split (candidate at 10%). Bakes the candidate artifact into the image,
# kind-loads it, applies the canary tier + both ingresses, swaps the base ingress.
make deploy-canary MODEL_VERSION=2.0.0

# 100% candidate. Stops the automated check loop first (todo 30), then raises
# canary-weight to 100. Deliberate gated step — never auto-promoted.
make promote

# 0% candidate. Deletes the canary Ingress/Deployment/Service and restores the
# base ingress (-> service-gateway; auth/rate-limit back). Idempotent.
make rollback
```

`deploy-canary` / `promote` / `rollback` are all **idempotent and safe to
re-run**; `rollback` converges to the clean stable state from any canary state
(@10%, promoted @100%, or partially torn down).

### Observing the split

The `model_version` is in the `/v1/predict` response body, so the split is
countable per-request (no auth needed during the canary):

```bash
for i in $(seq 1 100); do
  curl -s -X POST http://localhost:8080/v1/predict \
    -H 'Host: inferenceops.local' -H 'Content-Type: application/json' \
    -d '{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'
  echo
done | sort | uniq -c   # ~90% model_version 1.0.0, ~10% 2.0.0
```

or via the `inferenceops_request_count{model_version=...}` metric in Prometheus
(scraped from model-server `/metrics`, todo 20). The ~10% figure is statistical
(nginx rounds weight per-request); assert a range (e.g. 3–20%), not exactly 10%.

### Automated rollback (todo 30)

`scripts/deploy_canary.sh` will (in todo 30) start a `canary_check.sh` loop on a
30s interval that compares candidate-vs-stable p95 / error-rate / eval score
(filtered by the `model_version` label) and writes a `promote-ready` marker after
a clean window; on breach it runs `make rollback`. The loop is **terminal**
(exits on promote-ready) and `make promote` **stops it first** (kills the loop
PID at `${TMPDIR:-/tmp}/inferenceops-canary-check.pid` and clears the marker) so
a post-promote check cannot race the promotion or roll back a just-promoted
version. Any unexpected check error fails closed to rollback. todo 29 ships only
the manual `deploy-canary` / `promote` / `rollback` tooling; the loop-stopping
hooks in `promote.sh` / `rollback.sh` are no-ops until todo 30 starts a loop.
