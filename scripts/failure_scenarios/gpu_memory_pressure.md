# Failure Scenario: GPU Memory Pressure

> ## ⚠️ NOT EXECUTABLE HERE
>
> **This scenario is a DOCUMENTED PROCEDURE only — it cannot be run on this
> cluster.** The local platform is a single-node **CPU-only** kind cluster
> (`inferenceops`): there is **no NVIDIA GPU, no NVIDIA device plugin, and no
> DCGM exporter**, so the `DCGM_FI_DEV_*` metric series this scenario depends on
> are **absent**. The GPU alerts load cleanly into Prometheus but evaluate to no
> result and stay `inactive` forever (verified in todo 32). To exercise this
> scenario for real you need a GPU node group + `dcgm-exporter` (see
> "How it would be exercised" below).

## What this scenario is

A serving workload (e.g. the vLLM GPU backend, todo 42) consumes more GPU
framebuffer (VRAM) than is healthy — approaching or exceeding the device's
memory — risking `CUDA out of memory` errors, evictions, or `OOMKilled`
containers. The platform is expected to **detect** the pressure via DCGM metrics
+ alerts and give operators a clear **response** path.

## What WOULD happen (on a GPU cluster)

1. The NVIDIA **DCGM exporter** scrapes per-device framebuffer metrics:
   - `DCGM_FI_DEV_FB_USED` / `DCGM_FI_DEV_FB_FREE` (MiB used / free),
   - `DCGM_FI_DEV_GPU_UTIL` (compute utilisation %).
2. When VRAM usage stays high, the **`HighGpuMemoryUsage`** alert fires
   (`observability/prometheus/rules/gpu-alerts.yaml`):
   ```promql
   (DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)) > 0.9
   # for: 5m, severity: info
   ```
   and **`HighGpuUtilization`** (`DCGM_FI_DEV_GPU_UTIL > 90`, `for: 5m`, info)
   fires alongside sustained compute saturation.
3. A pod that actually exhausts VRAM is `OOMKilled` and **restarted by the
   kubelet** (`RestartPolicy=Always`) — the same self-heal path demonstrated by
   `crash_model_server.sh`, but driven by the kernel OOM killer rather than a
   liveness probe.
4. The **canary health check** (`scripts/canary_check.sh`, check 4) reads
   `DCGM_FI_DEV_FB_USED` but treats it as **INFORMATIONAL ONLY** — GPU memory
   never triggers an auto-rollback (a rollback cannot free GPU memory the way it
   can shed a latency/error regression).

## How to detect

| Signal | Where | Query / object |
|---|---|---|
| VRAM usage | Prometheus | `DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)` |
| Compute saturation | Prometheus | `DCGM_FI_DEV_GPU_UTIL` |
| Alerts | Alertmanager | `HighGpuMemoryUsage`, `HighGpuUtilization` (both `info`) |
| Dashboard | Grafana | GPU panels (todo 33) — empty on this cluster (no DCGM series) |
| SLO | runbook | GPU utilization SLO `avg(DCGM_FI_DEV_GPU_UTIL) < 90` |
| Pod state | kubectl | `kubectl get pods -n inferenceops` — look for `OOMKilled` / restarts |

## How to respond

1. **Confirm** the pressure: query the DCGM series above; check which `gpu`
   label (device) and which pod is responsible (`kubectl describe pod`).
2. **Shed load**: scale the serving workload down (`kubectl scale` / lower the
   HPA max) or reduce concurrent batch size so VRAM demand drops.
3. **Relieve contention**: evict or reschedule competing GPU workloads off the
   saturated device (cordon the node, `kubectl drain` with GPU-aware scheduling).
4. **Right-size the model**: serve a smaller / quantized variant, or raise the
   `nvidia.com/gpu` count / move to a larger GPU instance class (the runbook's
   HighGpuMemoryUsage section: add GPU nodes or a cheaper serving path).
5. **Verify recovery**: VRAM ratio drops below 0.9, the alert clears, and the
   serving pods are `Ready` with no further `OOMKilled` restarts.

See `docs/incident-runbook.md` → `### HighGpuMemoryUsage` and
`### HighGpuUtilization` for the operator-facing steps.

## Why it is NOT executable here

- **No GPU on the kind node** — `nvidia-smi` does not exist; no device to put
  under pressure.
- **No DCGM exporter / NVIDIA device plugin** — the `DCGM_FI_DEV_*` series are
  absent, so the alert expressions return no result and never fire (this is by
  design and was verified in todo 32: the PromQL is valid, the alerts stay
  `inactive`).
- The base model-server is **CPU-only** (`k8s/base/deployment-model-server.yaml`
  deliberately carries NO GPU nodeSelector/tolerations or `nvidia.com/gpu`
  resources — those belong in the prod overlay, todo 18).

## How it WOULD be exercised (prod / GPU cluster)

1. Provision a **GPU node group** (EKS `g5.2xlarge` / A10G — see
   `infra/terraform/modules/eks/`) with the **NVIDIA device plugin** DaemonSet
   (advertises `nvidia.com/gpu`, installs the `nvidia` runtimeClass) and the
   **dcgm-exporter** (emits `DCGM_FI_DEV_*`, scraped via a ServiceMonitor).
2. Deploy the GPU serving backend (vLLM, `serving/vllm/values.yaml`, todo 42)
   with `resources.limits["nvidia.com/gpu"]: 1` + `runtimeClassName: nvidia`.
3. Drive VRAM pressure (large/long-context requests, or an oversized
   `--gpu-memory-utilization`) until `HighGpuMemoryUsage` fires, then walk the
   response steps above and confirm the alert clears.

## Cross-references

- `observability/prometheus/rules/gpu-alerts.yaml` — the two GPU alert rules.
- `docs/incident-runbook.md` — `HighGpuUtilization` / `HighGpuMemoryUsage` sections + SLI/SLO.
- `scripts/canary_check.sh` — check 4 (GPU mem, informational only).
- `serving/vllm/values.yaml` — the GPU serving backend (todo 42).
- `crash_model_server.sh` — the executable OOMKill/crash self-heal analogue (CPU).
