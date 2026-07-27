# Kubernetes for ML workloads (todo 21)

How the Kubernetes concepts that make **serving ML models** reliable map onto the
actual InferenceOps manifests under `k8s/`. Every section names the concrete
manifest file and field it describes. The live assertions for the hardening
fields live in [`scripts/verify_k8s.sh`](../scripts/verify_k8s.sh) — run it
against the kind cluster (`scripts/verify_k8s.sh`) to verify the deployed
`model-server` actually carries each field.

The workload: `model-server` loads a fraud-detection model into memory and
serves `/v1/predict` / `/v1/batch` on port 8080
(`apps/model-server/src/inferenceops_model_server/server.py`). Model loading is
slow (seconds) and the process is stateful in memory — both facts drive every
Kubernetes decision below.

---

## 1. Pod lifecycle

A **Pod** is the unit Kubernetes schedules: one or more containers sharing a
network namespace and (optionally) volumes. A Pod moves through phases
`Pending → Running → Succeeded/Failed`; inside `Running`, each container is
`Waiting`, `Running`, or `Terminated`. Kubernetes never "heals" a container in
place — it kills the Pod and the controller (a `Deployment`) creates a new one.
For an ML server that means: *any* crash loses the loaded model and pays the
full reload cost on a fresh Pod, so we invest in probes and graceful shutdown
to make restarts rare and orderly.

| Concept | InferenceOps manifest |
|---|---|
| Pod template (labels, containers, probes, resources) | `k8s/base/deployment-model-server.yaml` → `spec.template` |
| Replica count / desired state | `spec.replicas: 2` (base), `3` in `k8s/overlays/prod/patch-model-server.yaml` |
| Per-app identity for RBAC | `k8s/base/serviceaccount.yaml` (`serviceAccountName: model-server` in the pod spec) |
| Namespace boundary | `k8s/base/namespace.yaml` (`inferenceops`) |

The Deployment controller reconciles observed state toward `spec.replicas`; the
rest of this doc is about *how* those Pods are probed, resourced, rolled,
spread, protected, scaled, exposed, configured, and placed.

---

## 2. Readiness vs liveness probes

Two distinct questions, two distinct probes — conflating them is the classic
ML-serving outage (a slow model load gets the Pod killed in a restart loop).

- **Liveness** (`/healthz`): "is the process alive and not wedged?" If it fails
  `failureThreshold` times, the kubelet **restarts the container**. It must NOT
  depend on the model being loaded — only on the process being up.
- **Readiness** (`/readyz`): "can this Pod serve traffic *right now*?" If it
  fails, the Pod is removed from Service endpoints (no traffic) but is **not**
  restarted. This is where slow startup lives.

`k8s/base/deployment-model-server.yaml`:

```yaml
readinessProbe:            # 503 until the model loads, 200 after
  httpGet: { path: /readyz, port: http }
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 30     # 30 x 5s ≈ 150s budget for a slow model load
livenessProbe:             # 200 once the process is up (model-independent)
  httpGet: { path: /healthz, port: http }
  initialDelaySeconds: 10
  periodSeconds: 20
  failureThreshold: 3
```

**Why `/readyz` returns 503 until the model loads:** the server starts the HTTP
listener immediately, then loads the model in the background. Until loading
finishes, `GET /readyz` answers `503` (and `/v1/predict` answers `404`); after,
`200`. The readiness probe polls `/readyz`, so the Service sends **no traffic
to a Pod whose model is still loading** — requests never hit a half-initialized
server. The generous `failureThreshold: 30` gives a large model ~150s to load
without the Pod ever being killed; the liveness probe (`/healthz`, 200 as soon
as the process is up) never fires during that window. See
`apps/model-server/src/inferenceops_model_server/server.py` (the `/readyz`
503→200 transition) and `apps/model-server/tests/test_server.py`
(`test_readyz_transitions_503_to_200_across_startup`).

The gateway (`k8s/base/deployment-gateway.yaml`) has no slow startup, so both
its probes hit the same `/healthz` endpoint.

`verify_k8s.sh` asserts the live Deployment's probe paths are exactly
`/readyz` (readiness) and `/healthz` (liveness).

---

## 3. Requests vs limits

- **requests** — what the container is *guaranteed*; the scheduler places the
  Pod on a node with this much free. Used for **bin-packing** and by the HPA's
  utilization math (`utilization = usage / request`).
- **limits** — the hard ceiling. Exceeding a CPU limit **throttles** (CFS
  quota); exceeding a memory limit gets the container **OOMKilled**.

`k8s/base/deployment-model-server.yaml`:

```yaml
resources:
  requests: { cpu: "2", memory: "4Gi" }   # guaranteed floor (scheduling + HPA base)
  limits:   { cpu: "4", memory: "8Gi" }   # hard ceiling (throttle / OOMKill)
```

Because `requests < limits`, the Pod is **Burstable** QoS: it can use idle CPU
up to 4 cores (bursty inference batches) while only reserving 2. The gateway is
lighter (`requests 250m/256Mi`, `limits 1/512Mi` in
`k8s/base/deployment-gateway.yaml`); prod bumps the model-server to
`requests 4/8Gi`, `limits 8/16Gi`
(`k8s/overlays/prod/patch-model-server.yaml`).

ML note: memory limits must cover the model weights **plus** per-request
activation/batch buffers — an undersized memory limit manifests as sporadic
`OOMKilled` restarts under load, not at idle.

`verify_k8s.sh` asserts both `requests` and `limits` (cpu + memory) are set on
the live container.

---

## 4. Rolling deployments + graceful shutdown

### Rolling update strategy

`k8s/base/deployment-model-server.yaml`:

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0   # never drop below desired replicas during a rollout
    maxSurge: 1         # bring up exactly one extra (new) Pod at a time
```

A rollout brings up one new Pod, waits for it to become **Ready** (readiness
probe passing — model loaded), then terminates one old Pod, and repeats. With
`maxUnavailable: 0` the fleet never loses capacity mid-rollout — critical when
each Pod takes ~minutes to reload a model.

### Graceful shutdown (preStop + terminationGracePeriodSeconds + SIGTERM)

When Kubernetes terminates a Pod it runs, **in order**:

1. Remove the Pod from Service endpoints (stop *new* traffic).
2. Run the **preStop** hook (blocking).
3. Send **SIGTERM** to PID 1.
4. Wait `terminationGracePeriodSeconds`, then **SIGKILL**.

`k8s/base/deployment-model-server.yaml`:

```yaml
terminationGracePeriodSeconds: 30
containers:
  - name: model-server
    lifecycle:
      preStop:
        exec:
          command: ["/bin/sh", "-c", "sleep 10"]
```

**Why the `sleep 10` preStop:** endpoint removal is asynchronous — kube-proxy
rules on other nodes can keep routing in-flight connections for a moment after
the Pod is de-listed. Sleeping 10s *before* SIGTERM lets that propagation
settle and in-flight requests drain, so no request is cut mid-prediction. Then
SIGTERM arrives and the server runs its own drain: the uvicorn lifespan installs
a SIGTERM handler that finishes in-flight batches and exits 0
(`apps/model-server/src/inferenceops_model_server/server.py`; proven by
`test_server.py`'s real-SIGTERM→exit-0 test). The 30s grace period bounds the
whole sequence (10s preStop + ≤20s drain) before SIGKILL. The gateway uses a
lighter `sleep 5` / `terminationGracePeriodSeconds: 15`
(`k8s/base/deployment-gateway.yaml`).

`verify_k8s.sh` asserts `lifecycle.preStop` exists,
`terminationGracePeriodSeconds >= 30`, and `maxUnavailable == 0` /
`maxSurge == 1` on the live Deployment.

---

## 5. Pod anti-affinity

Spreading replicas across failure domains so one node loss doesn't take out the
whole service.

`k8s/base/deployment-model-server.yaml` — **soft** (best-effort) rule:

```yaml
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname     # "distinct hosts"
          labelSelector:
            matchLabels:
              app.kubernetes.io/name: model-server
```

"Prefer to schedule model-server Pods on different hosts; if the cluster can't
(e.g. a 1-node kind cluster), schedule anyway." The prod overlay upgrades this
to a **hard** rule via a JSON6902 `replace`
(`k8s/overlays/prod/patch-model-server-antiaffinity.yaml`):

```yaml
- op: replace
  path: /spec/template/spec/affinity/podAntiAffinity
  value:
    requiredDuringSchedulingIgnoredDuringExecution:
      - topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels: { app.kubernetes.io/name: model-server }
```

Hard anti-affinity means a model-server Pod stays `Pending` rather than
doubling up on a host — correct for prod HA, wrong for a single-node kind box
(hence soft in base). `IgnoredDuringExecution` means already-running Pods are
not evicted if the rule later can't be satisfied.

`verify_k8s.sh` asserts the live Deployment has a podAntiAffinity rule
(preferred **or** required).

---

## 6. PodDisruptionBudget (PDB)

A PDB bounds **voluntary** disruptions (node drains, rolling upgrades, cluster
upgrades) — it does nothing for involuntary ones (node crash). It pairs with
`maxUnavailable: 0`: the rollout strategy governs *rollouts*, the PDB governs
*everything else that deliberately evicts Pods*.

`k8s/base/pdb.yaml`:

```yaml
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: model-server
```

At least one model-server Pod must remain available, so `kubectl drain` on a
node blocks until another replica is up — you can never drain the fleet to
zero. `minAvailable: 1` is the HA floor that matches `minReplicas: 2` in the
HPA.

`verify_k8s.sh` asserts the live `pdb/model-server` has `minAvailable >= 1`.

---

## 7. HPA vs cluster-autoscaler vs Karpenter

Three layers that scale **different things** — InferenceOps uses the first on
kind and documents the third for EKS:

| Layer | Scales | InferenceOps |
|---|---|---|
| **HPA** (HorizontalPodAutoscaler) | **Pods** (Deployment replicas) | active: `k8s/base/hpa.yaml` + `k8s/overlays/local/patch-hpa-custom-rps.yaml` |
| **cluster-autoscaler** | **Nodes** (adds/removes VMs to fit Pending Pods; ASG-based) | not used (superseded by Karpenter on EKS) |
| **Karpenter** | **Nodes** (provisions exactly-sized nodes directly from the cloud API, no ASG) | **EKS-only**, documented — see below |

### HPA (Pod autoscaling)

`k8s/base/hpa.yaml` (cpu + memory) merged with the local overlay's external-RPS
metric (`k8s/overlays/local/patch-hpa-custom-rps.yaml`) into **one** HPA:

```yaml
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: model-server }
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource: { name: cpu,    target: { type: Utilization, averageUtilization: 70 } }
    - type: Resource
      resource: { name: memory, target: { type: Utilization, averageUtilization: 80 } }
    - type: External                       # added by the local overlay (todo 20)
      external:
        metric: { name: inferenceops_rps }
        target: { type: AverageValue, averageValue: "30" }   # ~30 RPS per replica
```

The HPA evaluates **all** metrics and scales to satisfy the *most demanding*
one. The External metric `inferenceops_rps` is served by **prometheus-adapter**
from the scraped `inferenceops_request_count_total` counter (the scrape is
wired by the ServiceMonitor in
`k8s/overlays/local/servicemonitor-model-server.yaml`). Request rate is the
signal that actually tracks serving load; cpu/memory are co-signals. If
Prometheus/adapter is down the External metric reports `<unknown>` and the HPA
**degrades gracefully** to cpu/memory. Full design + the alternative KEDA
`ScaledObject` (opt-in, never alongside the HPA —
`k8s/overlays/local/scaledobject-model-server.yaml`) in
[`docs/autoscaling-strategy.md`](autoscaling-strategy.md).

### cluster-autoscaler vs Karpenter (node autoscaling)

When the HPA wants more Pods than the nodes can fit, the new Pods go `Pending`;
a **node** autoscaler then adds capacity:

- **cluster-autoscaler** watches for `Pending` Pods and adjusts node-group /
  ASG sizes. It inherits ASG constraints (instance types, AZ balance) and is
  comparatively slow.
- **Karpenter** watches the same `Pending` Pods but provisions nodes directly
  via the cloud API, choosing instance type/size to fit the actual workload
  (bin-packing), with node templates and disruption/consolidation. It is
  **EKS-only** — it needs a cloud provider, so it cannot run on kind.

InferenceOps realizes Karpenter as the optional GPU node group + Karpenter
submodule of the EKS Terraform module (todo 23,
`infra/terraform/modules/eks/`), gated by `var.enable_karpenter`; the GPU node
group's taint is exactly what the prod overlay tolerates (next section). On
kind there is no cloud provider to add nodes, so node autoscaling is documented
only.

`verify_k8s.sh` asserts the live HPA carries cpu + memory + an External metric.

---

## 8. Services vs Ingress

- A **Service** is a stable in-cluster virtual IP + DNS name over a set of Pods
  (which are ephemeral and re-IP'd on every restart). Type `ClusterIP` is
  cluster-internal only.
- An **Ingress** is L7 (HTTP) edge routing — host/path rules terminated by an
  ingress controller (nginx here) — that fronts Services for traffic from
  *outside* the cluster.

InferenceOps chains them: `Ingress → service-gateway → service-model-server`.

| Manifest | Role |
|---|---|
| `k8s/base/service-model-server.yaml` | `ClusterIP` `service-model-server:8080` → model-server Pods (internal; the gateway calls `http://service-model-server:8080`) |
| `k8s/base/service-gateway.yaml` | `ClusterIP` `service-gateway:8080` → gateway Pods |
| `k8s/base/ingress.yaml` | `ingressClassName: nginx`, host `inferenceops.local`, `path / → service-gateway:8080` |

The Ingress fronts the **gateway** (auth + rate-limit edge), never the
model-server directly — the model-server Service is deliberately not exposed at
the edge. On kind the nginx controller is published via `extraPortMappings`
(`8080:80`, `8443:443` — `infra/scripts/kind-config.yaml`, deployed by
`scripts/deploy_local.sh`), so the edge is reachable at `localhost:8080`.

---

## 9. ConfigMaps vs Secrets

Both inject configuration without baking it into the image; the difference is
intent and handling.

- **ConfigMap** — non-sensitive config. `k8s/base/configmap.yaml`
  (`inferenceops-config`): `MODEL_ID`, `MODEL_VERSION`, `RATE_LIMIT_RPS`,
  `LOG_LEVEL`.
- **Secret** — credentials. `k8s/base/secret.example.yaml` (`gateway-api-keys`,
  `API_KEYS`). The committed file is an **example with placeholder dev keys**;
  real values go in a git-ignored `secret.yaml`. Secrets are base64-*encoded*
  (not encrypted) at rest by default — keep them out of git and use a real
  secret store in prod.

Consumption is per-key via env refs, e.g. in
`k8s/base/deployment-model-server.yaml`:

```yaml
env:
  - name: MODEL_ID
    valueFrom:
      configMapKeyRef: { name: inferenceops-config, key: MODEL_ID }
```

and in `k8s/base/deployment-gateway.yaml`:

```yaml
env:
  - name: API_KEYS
    valueFrom:
      secretKeyRef: { name: gateway-api-keys, key: API_KEYS }   # Secret, never ConfigMap
```

Rule of thumb: if leaking it lets someone authenticate or spend money, it's a
Secret.

---

## 10. Node selectors, taints/tolerations, and GPU scheduling

Three placement mechanisms, composed for GPU workloads:

- **nodeSelector** — hard label match: the Pod only lands on nodes carrying the
  label.
- **taints + tolerations** — the *node* repels Pods; only Pods with a matching
  **toleration** may schedule there. (Anti-affinity keeps Pods apart; taints
  keep Pods *out*.)
- **`nvidia.com/gpu` resource** — the NVIDIA device plugin advertises an
  extended resource; requesting it schedules the Pod onto a GPU node and binds
  a device. GPU requests must equal limits, and GPUs are not fractionally
  shared by default.

The **base** manifest deliberately has **no** GPU scheduling — the default
fraud model is CPU-only. The **prod overlay**
(`k8s/overlays/prod/patch-model-server.yaml`) makes the Deployment GPU-*ready*:

```yaml
spec:
  template:
    spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"        # pin to GPU nodes
      tolerations:
        - key: nvidia.com/gpu                 # tolerate the GPU node-group taint
          operator: Equal
          value: present
          effect: NoSchedule
      containers:
        - name: model-server
          resources:
            requests: { cpu: "4", memory: "8Gi" }
            limits:   { cpu: "8", memory: "16Gi" }
            # nvidia.com/gpu: 1  # UNCOMMENT (request AND limit) for GPU workloads (e.g. vLLM)
```

The matching node side lives in the EKS Terraform (todo 23,
`infra/terraform/modules/eks/`): the optional GPU managed node group
(`g5.xlarge`, `ami_type = BOTTLEROCKET_x86_64_NVIDIA`, gated by
`var.enable_gpu_node_group`) is **tainted** `nvidia.com/gpu=present:NoSchedule`
and labeled so `nvidia.com/gpu.present=true` — exactly the taint the prod
toleration matches and the label the prod nodeSelector matches. So: CPU
workloads are repelled from expensive GPU nodes (the taint), and the
model-server (with the toleration + nodeSelector) is pinned onto them. To
actually bind a GPU, uncomment `nvidia.com/gpu: 1` in **both** requests and
limits.

On kind there are no GPUs and no taints, so the local overlay
(`k8s/overlays/local/`) leaves scheduling to the base soft anti-affinity and
sets `imagePullPolicy: Never` (images are `kind load`'d — there is no registry).

The node side of this composition (the tainted `g5.xlarge` group, the NVIDIA
device plugin that advertises `nvidia.com/gpu`) is provisioned by the EKS
Terraform, and the model-server's S3 artifact reads ride IRSA rather than
static keys: [aws-deployment.md](aws-deployment.md) §5 (IRSA) and §6 (GPU
node group) walk through both.

---

## 11. imagePullPolicy

`k8s/base/deployment-model-server.yaml` sets `imagePullPolicy: IfNotPresent`
(pull only if the image isn't already on the node); the local overlay patches it
to `Never` (images are `kind load docker-image`'d onto the nodes — there is no
registry, so any pull would fail with `ErrImageNeverPull`). Prod keeps
`IfNotPresent` against ECR. `verify_k8s.sh` accepts either `IfNotPresent` or
`Never` (both are valid "don't require a live registry pull" policies) and
fails on `Always`-or-unset.

---

## 12. Observability hook: ServiceMonitor

`k8s/overlays/local/servicemonitor-model-server.yaml` tells the
kube-prometheus-stack Prometheus to scrape the model-server's `/metrics`
(port `http`, `interval: 15s`). This is what feeds the `inferenceops_rps`
external metric the HPA scales on (§7) and the canary checks (todo 30). It
carries the label `release: kube-prometheus` because that is what the stack's
default `serviceMonitorSelector` matches. `verify_k8s.sh` asserts the
`servicemonitor/model-server` exists and has endpoints.

---

## Verifying it all on the live cluster

```bash
scripts/verify_k8s.sh                 # 15 assertions, exit 0 iff all PASS
```

The script queries the **live** cluster (`kubectl --context kind-inferenceops
-n inferenceops get … -o json` + `jq`) — never a cached copy — and prints
`PASS`/`FAIL: <reason>` per hardening field: probes (`/readyz`, `/healthz`),
requests+limits, `rollingUpdate {maxUnavailable:0, maxSurge:1}`, `preStop`,
`terminationGracePeriodSeconds >= 30`, podAntiAffinity, imagePullPolicy,
PDB `minAvailable >= 1`, HPA cpu+memory+External, and the ServiceMonitor.
Remove any field (e.g. `kubectl … patch deployment model-server --type=json
-p='[{"op":"remove","path":"/spec/template/spec/containers/0/lifecycle"}]'`)
and the corresponding check flips to `FAIL` with a non-zero exit; re-apply
`kubectl apply -k k8s/overlays/local` to restore.

---

## See also

- [architecture.md](architecture.md): system topology, the request path, the model lifecycle, and the observability pipeline these manifests feed.
- [aws-deployment.md](aws-deployment.md): the EKS Terraform that provisions the nodes these pods run on (IRSA §5, GPU node group §6, prod overlay deploy §7).
- [autoscaling-strategy.md](autoscaling-strategy.md): the external-RPS metric path, HPA vs KEDA, and Karpenter node autoscaling in depth.
- [observability.md](observability.md): the metrics/traces/logs stack and SLI/SLO contract built on these probes and metrics.
- [incident-runbook.md](incident-runbook.md): alert response procedures.
- [serving/fastapi/deployment.md](../serving/fastapi/deployment.md): the step-by-step deploy guide (kind + EKS) for the workload described here.
