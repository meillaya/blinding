# Autoscaling strategy (todo 20)

InferenceOps scales the **model-server** serving Deployment on **request rate**
(the signal that actually tracks serving load), with cpu/memory as co-signals.
Two interchangeable mechanisms are provided — pick **ONE** per Deployment:

| Mechanism | Manifest | Status |
|---|---|---|
| **Custom-RPS HPA** (default) | `k8s/base/hpa.yaml` + `k8s/overlays/local/patch-hpa-custom-rps.yaml` | active in the local overlay |
| **KEDA ScaledObject** (alternative) | `k8s/overlays/local/scaledobject-model-server.yaml` | opt-in, NOT in `resources` |

> **One scaler per Deployment.** The HPA and the KEDA ScaledObject both write the
> Deployment's `scale` subresource. Running both makes them fight (oscillating
> replica counts). Apply the ScaledObject **instead of** the HPA, never alongside.

Node-level autoscaling (**Karpenter**) is **EKS-only** and is documented here as
a reference; it is **not** deployed on kind (there is no cloud provider to add
nodes). See [Karpenter](#karpenter-node-autoscaling-eks-only).

---

## 1. Custom-RPS HPA (default)

The base HPA (`k8s/base/hpa.yaml`) scales on **cpu 70% + memory 80%** (2..10).
The local overlay (`patch-hpa-custom-rps.yaml`) **adds an external RPS metric**
to the *same* HPA (one HPA, three metrics — there is deliberately no second HPA):

```yaml
- type: External
  external:
    metric:
      name: inferenceops_rps
    target:
      type: AverageValue
      averageValue: "30"      # ~30 RPS per replica
```

Data path:

```
model-server /metrics ──(ServiceMonitor)──> Prometheus ──(prometheus-adapter)──>
   external.metrics.k8s.io/v1beta1  inferenceops_rps  ──> HPA
```

- **ServiceMonitor** `k8s/overlays/local/servicemonitor-model-server.yaml` —
  label `release: kube-prometheus` so the default kube-prometheus-stack
  Prometheus selects it; scrapes port `http` `/metrics` every 15s.
- **prometheus-adapter** `observability/prometheus-adapter/values.yaml` —
  `rules.external` maps `sum(rate(inferenceops_request_count_total[1m])) by
  (namespace,pod)` to the external metric `inferenceops_rps`. Install:
  ```bash
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
  helm upgrade --install prometheus-adapter prometheus-community/prometheus-adapter \
    --version 5.3.0 -n monitoring --create-namespace \
    -f observability/prometheus-adapter/values.yaml
  ```
- The HPA **sums** the adapter's per-pod series to the deployment's total RPS and
  targets an AverageValue per replica, so `desiredReplicas = ceil(totalRPS / 30)`.

> **Metric name note:** prometheus_client exposes the request Counter with the
> OpenMetrics `_total` suffix, so the scraped series is
> `inferenceops_request_count_total` (the shorthand `inferenceops_request_count`
> refers to the same counter).

### Graceful degradation (verified — `/tmp/qa/t20_hpa_degraded.txt`)

If prometheus-adapter/Prometheus is unavailable, the external metric fetch
**fails**. The Kubernetes `autoscaling/v2` controller's fail-safe is to set
`ScalingActive=False (FailedGetExternalMetric)` and **hold** the current replica
count rather than scale on incomplete data — it does **not** silently drop the
failed metric and scale on the survivors. Crucially, the **cpu/memory resource
metrics still resolve** (they never degrade to `<unknown>`), and the HPA
**self-heals to full scaling the instant the metric recovers**. Keeping
cpu+memory in the same HPA means they stay healthy/visible and, in the normal
case, co-drive scaling (the HPA scales on the **max** of all metrics — so an RPS
spike scales the app even when cpu is low, as the happy QA shows: cpu ≤ 9% yet
2 → 6 on RPS).

---

## 2. KEDA ScaledObject (alternative)

`k8s/overlays/local/scaledobject-model-server.yaml` scales on the **same** RPS
signal via KEDA's `prometheus` trigger (KEDA queries Prometheus directly):

```yaml
triggers:
  - type: prometheus
    metricType: AverageValue
    metadata:
      serverAddress: http://kube-prometheus-kube-prome-prometheus.monitoring.svc:9090
      query: sum(rate(inferenceops_request_count_total{namespace="inferenceops"}[1m]))
      threshold: "30"          # desiredReplicas = ceil(totalRPS / 30)
      activationThreshold: "1"
```

Install KEDA (one-time):

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --version 2.20.1 -n keda --create-namespace
```

### Switching HPA ⇄ KEDA

```bash
# HPA -> KEDA
kubectl --context kind-inferenceops -n inferenceops delete hpa model-server
kubectl --context kind-inferenceops apply -f k8s/overlays/local/scaledobject-model-server.yaml

# KEDA -> HPA (restore the platform default)
kubectl --context kind-inferenceops delete -f k8s/overlays/local/scaledobject-model-server.yaml
kubectl --context kind-inferenceops apply -k k8s/overlays/local   # recreates the HPA
```

### ⚠ external.metrics.k8s.io is mutually exclusive

`prometheus-adapter` and KEDA's metrics-apiserver **both register the single
cluster-scoped APIService `v1beta1.external.metrics.k8s.io`** — only one
provider can serve it. Installing KEDA on a cluster that already has
prometheus-adapter fails with an ownership conflict on that APIService.

Because they are **alternatives**, time-share the provider with the scaler:

- **HPA active** → prometheus-adapter serves the external metric; KEDA's
  `metricsServer` must not be registered (install KEDA with
  `--set metricsServer.enabled=false` if you want the operator present but idle).
- **KEDA active** → KEDA serves the external metric; uninstall/silence
  prometheus-adapter (nothing consumes its metric while the HPA is deleted).

The live QA in this repo time-shared the provider: prometheus-adapter for the
HPA windows, then KEDA for the ScaledObject window, then restored
prometheus-adapter + HPA as the final healthy state.

---

## 3. Spike load generator

`scripts/spike_load.sh` (wired to `make load-test-spike`) drives a **bounded**,
**killable** burst of `/v1/predict` at the kind ingress (Host `inferenceops.local`,
`X-API-Key: test-key`) using parallel `curl` loops — **no k6 dependency**. ~1/12
requests are deliberately malformed (wrong feature count → HTTP 422) to prove
error traffic is counted and does not crash the scaler. It self-terminates after
`$DURATION` (default 75s) and tears down all workers on Ctrl-C/exit.

```bash
make load-test-spike                 # 24 workers, 75s
DURATION=120 WORKERS=32 scripts/spike_load.sh
```

### Timing / stabilization (avoid flaky assertions)

- `rate(...[1m])` needs ~60s of sustained traffic to reach the true RPS; the HPA
  syncs every 15s, so scale-up is visible ~45–75s after the spike starts.
- Scale-down waits for the rate to decay (~60s) **plus** the HPA
  `behavior.scaleDown.stabilizationWindowSeconds` (set to **60s** in the local
  overlay for fast, observable scale-down; the default is **300s** and is kept
  in staging/prod for conservative scale-down). Do **not** assert an exact
  replica count at an exact instant — assert the 2 → 5+ → 2 trend.

---

## Karpenter (node autoscaling, EKS-only)

**Karpenter** provisions/removes **nodes** in response to pending pods (it
replaces the legacy cluster-autoscaler on EKS, provisioning the right EC2
instance type directly from the node pool). It is a **node-level** complement to
the **pod-level** HPA/KEDA above: the HPA adds *pods*; if those pods are Pending
for lack of capacity, Karpenter adds *nodes*.

Karpenter requires a cloud provider (it calls EC2) and is therefore **not
deployable on kind** (single static node, no cloud controller). It is realized
as EKS Terraform in the infra milestone (todo 23) and referenced here only. A
typical EKS `NodePool` selects GPU instance types for the model-server's
`nvidia.com/gpu` requests (see the prod overlay's GPU nodeSelector/toleration).

```
pod autoscaling (this todo):  HPA / KEDA  -> replica count
node autoscaling (EKS, todo 23): Karpenter -> node count (Pending pods -> nodes)
```

---

## kind prerequisite: metrics-server

kind does **not** ship `metrics-server`, so the HPA's **cpu/memory** targets
report `<unknown>` until it is installed (the custom-RPS external metric works
regardless, via prometheus-adapter). For cpu/memory scaling on kind:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json -p \
  '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP"}]'
```

(`--kubelet-insecure-tls` is required because kind's kubelet cert is not signed
for the in-cluster DNS name; acceptable for local dev only.)
