# Cost analysis

Every measured number on this page comes from the real local benchmark
([../evals/reports/benchmark.md](../evals/reports/benchmark.md), generated
2026-07-27 from k6/locust runs plus live Prometheus). Pricing is public
us-east-1 on-demand. Nothing here is a projection dressed up as a measurement:
the GPU row is documented, not measured, because there is no GPU on the kind
host, and the page says so wherever it applies.

## Cost model

One full node billed per replica. That is deliberately conservative (it ignores
bin-packing several replicas onto one node), so it overstates cost and keeps
comparisons honest.

| Node | Instance | $/hr (us-east-1 on-demand) |
|---|---|---|
| CPU | m6i.2xlarge (8 vCPU / 32 GiB) | **$0.384** |
| GPU | g5.2xlarge (A10G, 24 GiB VRAM) | **$1.212** |

```
cost / 1k requests = replicas × node_$hr / (RPS × 3600) × 1000
```

## Measured cost per 1k requests

Straight from the benchmark table (replicas = peak `kube_deployment_status_replicas`
over each run window, bracketed +90s past the last sample because HPA scale-up
lags the RPS metric ~30–50s):

| Scenario | Replicas | RPS | p95 (ms) | Error rate | Cost / 1k reqs | Throughput / replica |
|---|---|---|---|---|---|---|
| baseline CPU (smoke, 2 VUs) | 2 | 37.1 | 54.4 | 0.00% | **$0.0058** | 18.6 |
| HPA enabled (spike, 70 VUs) | 6 | 2897.5 | 65.6 | 93.04% (by design) | **$0.0002** | 482.9 |
| soak (8 VUs, 90 s) | 5 | 137.9 | 60.6 | 0.00% | **$0.0039** | 27.6 |
| locust (50 users, think 0.5–1.5 s) | 2 | 45.4 | 59 | 0.00% | **$0.0047** | 22.7 |
| canary candidate (2.0.0, 10% split) | 1 | 2.7 | 73.8 | 0.00% | **$0.0390** | 2.7 |
| vLLM / GPU LLM | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) | documented (no GPU) |

Reading the rows:

- **The spike row is the cheapest per request** ($0.0002) because 2897.5 RPS
  amortizes six billed nodes. Its 93.04% error rate is the gateway shedding load
  on purpose (the intended spike signal, not an SLO breach), so read the row as
  "cost of the capacity that survived," not as a serving configuration.
- **The canary row is the most expensive** ($0.0390) by artifact, not by design:
  one replica serves 2.7 RPS (the candidate's 10% slice of ~38 blended RPS)
  while billing a whole node. It is a transient canary state, never a steady
  state. The benchmark notes this rather than hiding it.
- **Baseline vs locust, same 2 replicas:** more RPS means cheaper per request
  ($0.0058 at 37.1 RPS → $0.0047 at 45.4 RPS). Cost per request falls as a
  replica's fixed node cost spreads over more traffic.
- **Cold start and model load are in the benchmark too:** 7s cold start
  (creationTimestamp → Ready, worst pod) and 0.844s model load
  (`inferenceops_model_load_seconds`) for every CPU scenario. They do not enter
  the $/1k formula but they price the *reactions* below (spot interruptions,
  scale-up lag). See [autoscaling-strategy.md](autoscaling-strategy.md#4-cold-starts-and-model-load-time).

## CPU vs GPU: what the 3.2× price buys

A g5.2xlarge costs $1.212/hr against $0.384/hr for the m6i.2xlarge, 3.2× the
node price. The GPU does not buy a cheaper node. It buys throughput per replica,
which can make the *request* cheaper. An illustration from the formula (not a
measured row; the GPU benchmark row is documented because DCGM series are absent
on this host):

| Serving 100 RPS with | Replicas (30 RPS/replica target) | Node $/hr | Cost / 1k reqs |
|---|---|---|---|
| CPU (m6i.2xlarge) | 4 | 4 × $0.384 = $1.536 | $0.0043 |
| GPU (g5.2xlarge), if one replica holds 100 RPS | 1 | $1.212 | $0.0034 |

The crossover is entirely model-dependent: one GPU replica's capacity spans
orders of magnitude between a quantized 0.5B model and a 70B model, so the
30-RPS-per-replica target measured for the CPU fraud detector does not transfer.
Re-derive it from a real GPU benchmark before sizing a GPU pool. The mechanics
of scaling that pool (VRAM as the bottleneck, slow node provisioning) are in
[GPU autoscaling hardness](autoscaling-strategy.md#5-gpu-autoscaling-hardness).

## Spot nodes

Spot capacity trades price for availability:

- **Savings.** Spot typically runs 60–90% below on-demand for these families
  (the exact discount varies by instance type, region, and time; AWS sets it
  per capacity pool). At a 70% discount the $0.384 CPU node bills ~$0.115/hr
  and the $1.212 GPU node ~$0.364/hr, which flows straight through the cost
  formula above.
- **Interruption risk.** AWS gives a 2-minute warning, then reclaims the
  instance. The pod is evicted, and its replacement pays the full cold start
  again: 7s to Ready plus 0.844s model load on this platform. For the small
  fraud-detector artifact that is cheap, so spot is a good fit for the CPU
  pool's burst capacity. For an LLM with multi-gigabyte weights, the reload
  dominates and can outrun an alert window, so GPU spot wants warm pools or
  Karpenter consolidation, not naive eviction.
- **Wiring.** Karpenter expresses this as `capacityType: spot` on a `NodePool`
  (EKS only, see [autoscaling-strategy.md](autoscaling-strategy.md#karpenter-node-autoscaling-eks-only)).
  A safe shape is the HA floor (minReplicas 2) on on-demand nodes, with spot
  capacity absorbing everything above the floor.

## Autoscaling savings

The benchmark shows what the autoscaler actually saves:

- **Scale-to-min after the spike.** The HPA took model-server 2 → 6 for the
  spike, then recovered to min 2 within ~60–90s of traffic stopping (the local
  overlay's `scaleDown.stabilizationWindowSeconds: 60`; staging/prod keep the
  conservative 300s default). Idle time at peak size is bounded by that window.
- **Static provisioning would cost 3× the baseline.** Holding the spike's six
  replicas against baseline traffic (37.1 RPS) would bill
  6 × $0.384 / (37.1 × 3600) × 1000 = **$0.0173 per 1k requests**, against the
  measured **$0.0058** at 2 replicas. Provisioning for the peak and serving the
  average wastes the difference, and the HPA does not.
- **KEDA adds scale-to-zero for bursty workloads.** The ScaledObject here keeps
  the HA floor of 2 for online serving, but KEDA supports `minReplicaCount: 0`
  (with `activationThreshold` gating the wake-up), which is the right shape for
  batch or dev endpoints that sit idle for hours. A workload idle 12h/day at 2
  replicas bills $0.384 × 2 × 12 / 24 = $0.384/day for nothing; scale-to-zero
  removes it. The two scalers are alternatives, never both active at once (they
  fight over the `scale` subresource; see
  [autoscaling-strategy.md](autoscaling-strategy.md)).
