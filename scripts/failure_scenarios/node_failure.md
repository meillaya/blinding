# Failure Scenario: Node Failure

> ## ⚠️ NOT EXECUTABLE HERE
>
> **This scenario is a DOCUMENTED PROCEDURE only — it cannot be run
> meaningfully on this cluster.** The local platform is a **single-node** kind
> cluster: the one node (`inferenceops-control-plane`) hosts the control plane
> AND every workload. Stopping it takes down the API server and **all** pods at
> once — a full-cluster outage, not a *partial* node failure. There is **no
> second node** for pods to reschedule onto, so the graceful self-healing this
> scenario verifies (pods recreated on a *surviving* node) cannot be observed.
> To exercise it you need a multi-node cluster / EKS node group (see "How it
> would be exercised" below).

## What this scenario is

A worker node is lost (hardware failure, kernel panic, network partition, or a
terminated cloud instance). The platform is expected to **detect** the `NotReady`
node, **reschedule** the affected pods onto surviving nodes, and **keep serving**
predictions throughout — without operator intervention for the common case.

## What WOULD happen (on a multi-node cluster)

1. The kubelet stops posting heartbeats; after the node-monitor grace period
   (~40s) the node controller marks the node **`NotReady`**.
2. After the pod-eviction timeout (default ~5m for a truly unreachable node), the
   node controller **evicts** the pods bound to the dead node (they go
   `Terminating` / `Unknown`).
3. The **Deployment / ReplicaSet controllers** see the model-server replicas drop
   below the desired count and **create replacement pods**, which the scheduler
   places on **surviving Ready nodes**.
4. **`podAntiAffinity`** (preferred, `topologyKey: kubernetes.io/hostname`) in
   `k8s/base/deployment-model-server.yaml` spreads the 2 model-server replicas
   across distinct nodes, so a single node loss takes out **at most one** replica
   — the other keeps serving while the replacement schedules. The gateway
   (2 replicas) and KServe predictors spread the same way.
5. The **HPA** (min 2) keeps the replica floor; once the replacement is `Ready`,
   the Service has full endpoints again and `/v1/predict` keeps serving
   `model_version=1.0.0`.

## How to detect

| Signal | Where | Query / command |
|---|---|---|
| Node condition | kubectl | `kubectl get nodes` (a node `NotReady`) |
| Node condition metric | Prometheus | `kube_node_status_condition{condition="Ready",status="true"} == 0` |
| Alert | Alertmanager | `KubeNodeNotReady` (kube-prometheus-stack, ships by default) |
| Pod churn | kubectl | `kubectl get pods -n inferenceops -o wide` — pods `Terminating`/`Unknown` on the dead node, new pods on survivors |
| Serving | curl | `/v1/predict` keeps returning 200 (at least one replica survives) |

## How to respond

1. **Confirm** the node is gone vs transient: `kubectl describe node <node>`
   (conditions, last heartbeat), check the cloud provider / VM health.
2. **Trust the self-heal** for the common case: the Deployment recreates pods on
   survivors automatically. Verify replicas return to the desired count and the
   Service has full endpoints.
3. **Force eviction** if pods are stuck `Terminating`/`Unknown` on an
   unrecoverable node: `kubectl delete pod <pod> --grace-period=0 --force`, or
   `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data` after
   `kubectl cordon <node>`.
4. **Replace the node**: bring a new node into the group (EKS managed node group
   auto-replaces a terminated instance; on-prem, join a new kubelet). Uncordon
   once `Ready`.
5. **Verify recovery**: all nodes `Ready`, model-server + gateway at their desired
   replicas spread across nodes, `/v1/predict` serving, no `Unknown` pods.

## Why it is NOT executable here

- **Single node = the whole cluster.** `docker stop inferenceops-control-plane`
  (or `kind delete cluster`) removes the ONLY node: the API server, the data
  plane, and every pod die together. That is a **full outage**, not a partial
  node failure, and there is no surviving node to demonstrate rescheduling onto.
- **No anti-affinity spread to observe.** With one node, both model-server
  replicas are co-located (the preferred anti-affinity cannot spread them), so a
  node loss takes out both at once — the graceful "one survives" behaviour needs
  ≥ 2 nodes.
- The recovery primitives themselves (Deployment self-heal, liveness/restart) ARE
  exercised on this cluster by the **executable** `crash_model_server.sh`
  (container-level restart) — node failure is the node-level analogue that needs
  a real multi-node topology.

## How it WOULD be exercised (prod / multi-node cluster)

1. Run on a **multi-node** cluster (kind with multiple `nodes:` in the cluster
   config, or an EKS node group — `infra/terraform/modules/eks/`,
   `docs/aws-deployment.md`).
2. **Trigger** a node loss: `kubectl cordon <node> && kubectl drain <node>`
   (graceful) or terminate the EC2 instance / `docker stop <node-container>`
   (abrupt).
3. **Observe**: `kubectl get nodes` → one `NotReady`; `kubectl get pods
   -n inferenceops -o wide` → affected pods evicted, replacements `Pending` →
   `Running` on surviving nodes; `/v1/predict` keeps returning 200 throughout.
4. **Confirm** the anti-affinity spread (`kubectl get pods -o wide` shows
   model-server replicas on distinct hostnames) and that the HPA held the min-2
   floor, then replace the node and uncordon.

## Cross-references

- `k8s/base/deployment-model-server.yaml` — `replicas: 2`, `podAntiAffinity`
  (hostname spread), `RollingUpdate maxUnavailable: 0`, liveness/readiness probes.
- `k8s/base/hpa.yaml` + `k8s/overlays/local/patch-hpa-custom-rps.yaml` — min-2 floor.
- `docs/kubernetes-ml-workloads.md` — manifest field rationale (spread, probes).
- `docs/aws-deployment.md` / `infra/terraform/modules/eks/` — the prod multi-node topology.
- `crash_model_server.sh` — the executable container-level self-heal analogue.
