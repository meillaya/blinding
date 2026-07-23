# InferenceOps Incident Runbook

Operational reference for the InferenceOps serving platform: SLI/SLO
definitions, a response entry for every Prometheus alert rule, the rollback
procedure, and a sample blameless postmortem.

Sources of truth this document tracks:

- Alert rules (todo 32): `observability/prometheus/rules/*.yaml`
- Dashboards (todo 33): `observability/grafana/dashboards/*.json`
- Rollback (todo 30): `scripts/rollback.sh`, `scripts/canary_check.sh`, `make rollback`
- Metrics (todo 8/35): `apps/model-server/src/inferenceops_model_server/metrics.py`

Coverage is enforced: `scripts/check_runbook_coverage.sh` fails CI-style if any
alert name in `observability/prometheus/rules/*.yaml` lacks a `### <AlertName>`
entry below. Add a runbook entry in the same change that adds an alert rule.

## How to use this runbook

1. An alert fires. Find its entry under [Alert runbook](#alert-runbook) by the
   exact alert name (the name in the alert notification).
2. Follow the numbered **Response** steps in order. Each entry links the
   Grafana dashboard to open first.
3. If a canary is in flight, prefer the automated path: `canary_check.sh`
   rolls back on breach or ambiguity. To act by hand, run `make rollback`
   (see [Rollback procedure](#rollback-procedure)).
4. After any user-facing impact, open a postmortem using the template in
   [Sample postmortem](#sample-postmortem-blameless-latency-regression).

Severity handling:

| Severity | Meaning | Response time |
|---|---|---|
| `critical` | User-facing error budget burning now | Immediate, page on-call |
| `warning` | SLO breach or trending toward one | Next working interval, page if it persists past 15 min |
| `info` | Capacity signal, no user impact by itself | Business hours |

## SLI definitions

All four SLIs are computed from metrics the model server already exports
(`inferenceops_*`, todo 8/35) plus the DCGM exporter where GPUs are present.
Counters are stored with the `_total` suffix by `prometheus_client`.

| SLI | Metric | PromQL |
|---|---|---|
| **Latency** (p95, end-to-end) | `inferenceops_request_duration_seconds` (Histogram, label `model_version`) | `histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket[5m])) by (le))` |
| **Error rate** | `inferenceops_error_count_total`, `inferenceops_request_count_total` (Counters) | `sum(rate(inferenceops_error_count_total[5m])) / sum(rate(inferenceops_request_count_total[5m]))` |
| **Throughput** | `inferenceops_request_count_total` (Counter, labels `method`, `status`, `model_version`) | `sum(rate(inferenceops_request_count_total[5m]))` |
| **GPU utilization** | `DCGM_FI_DEV_GPU_UTIL` (DCGM exporter; series absent on CPU-only nodes) | `avg(DCGM_FI_DEV_GPU_UTIL)` |

Per-version views append `by (le, model_version)` (latency) or filter
`{model_version="<v>"}` (errors). The canary check uses exactly those
per-version queries to compare candidate vs stable.

## SLO definitions

Each SLO is measured with the same expression as its alert, so the alert and
the SLO can never disagree about the number.

| SLO | Target | Window | Measured as | Alert |
|---|---|---|---|---|
| Latency | p95 < 500 ms in 99.5% of 5-minute windows | 30 days rolling | `histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket[5m])) by (le)) < 0.5` per window | `HighP95Latency` |
| Errors | error rate < 1% in 99.5% of 5-minute windows | 30 days rolling | `sum(rate(inferenceops_error_count_total[5m])) / sum(rate(inferenceops_request_count_total[5m])) < 0.01` per window | `HighErrorRate` |
| Throughput | serve >= 50 req/s while the latency and error SLOs hold | sustained (capacity floor, exercised by k6 smoke, todo 37) | `sum(rate(inferenceops_request_count_total[5m]))` | none (capacity is tested offline; a collapsed rate shows up as missing series on `InferenceOps / Inference Overview`) |
| GPU utilization | < 90% sustained where GPUs are deployed | 5-minute windows | `avg(DCGM_FI_DEV_GPU_UTIL) < 90` | `HighGpuUtilization`, `HighGpuMemoryUsage` (both `info`) |

**Error budget.** 99.5% over 30 days leaves 0.5%, about 216 minutes of breach
(roughly 7 minutes per day). When the 30-day burn is exhausted, freeze risky
deploys and model promotions until the window recovers.

**Current alerting shape.** The rules are single-window (5m rate, `for: 2m`).
They catch sustained breaches, not 4-minute fast-burn spikes. Multi-window,
multi-burn-rate recording rules are tracked as follow-up work; until then, a
short spike that stays under 2 minutes will not page but still counts against
the budget on the dashboards.

## Alert runbook

One entry per rule in `observability/prometheus/rules/*.yaml`.

### HighP95Latency: P95 inference latency above 500 ms

- **Severity:** warning
- **Rule file:** `observability/prometheus/rules/latency-alerts.yaml`
- **Expression:** `histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket[5m])) by (le)) > 0.5` `for: 2m`
- **SLO:** latency (p95 < 500 ms)
- **Dashboards:** Grafana `InferenceOps / Latency Breakdown` (source `observability/grafana/dashboards/latency-breakdown.json`) first, then `InferenceOps / Inference Overview`

**What it means.** The blended 95th-percentile end-to-end request latency has
exceeded 500 ms for two consecutive minutes. Because the quantile is blended
across model versions, a slow canary serving even 10% of traffic can push the
global p95 into the slow tail and trip this alert.

**Response.**

1. Open `InferenceOps / Latency Breakdown`. Identify the dominant stage:
   queue wait, model load, or inference.
2. Split by version to localize the regression:
   `histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket[5m])) by (le, model_version))`.
   If one `model_version` owns the tail, a deploy or canary is the likely cause.
3. If a canary is in flight: `canary_check.sh` (30s loop) auto-rolls back when
   candidate p95 > stable p95 x 1.20. To act manually, run `make rollback`
   and confirm the base ingress is restored (see [Rollback procedure](#rollback-procedure)).
4. Check capacity: `kubectl -n inferenceops get hpa model-server`. If replicas
   are pinned at the max with high CPU, this is a capacity problem: raise the
   HPA ceiling or shed load.
5. Check for model-reload storms: spikes in `inferenceops_model_load_seconds`
   mean pods are restarting and re-loading the artifact (crash-loop, OOM).
   Inspect `kubectl -n inferenceops get events` and pod logs in Loki
   (`{namespace="inferenceops", pod=~"model-server-.*"}`).
6. If the error SLO is burning too, treat it as `HighErrorRate` (critical) and
   roll back the most recent change now, debug after.

### HighErrorRate: Inference error rate above 1 %

- **Severity:** critical
- **Rule file:** `observability/prometheus/rules/error-rate-alerts.yaml`
- **Expression:** `(sum(rate(inferenceops_error_count_total[5m])) / sum(rate(inferenceops_request_count_total[5m]))) > 0.01` `for: 2m`
- **SLO:** errors (< 1%)
- **Dashboards:** Grafana `InferenceOps / Inference Overview` (Error rate panel, source `observability/grafana/dashboards/inference-overview.json`), then `InferenceOps / Latency Breakdown`

**What it means.** More than 1% of requests are failing (server errors,
batch timeouts, bad input) for two consecutive minutes. With no traffic the
ratio yields no series, so this alert only fires under real load.

**Response.**

1. Open `InferenceOps / Inference Overview`. Confirm the error rate and the
   request rate together (a falling request rate with steady errors points at
   an upstream problem, not the model server).
2. Break errors down by status and version:
   `sum(rate(inferenceops_request_count_total{status!~"2.."}[5m])) by (status, model_version)`.
   - `500`: unhandled server error. Check model-server logs in Loki.
   - `504`: batch timeout. Check `inferenceops_batch_size` for oversized
     payloads; consider lowering the batch ceiling or raising the timeout.
   - `4xx`: client-side bad input. Usually not an incident; confirm the spike
     is not an auth or schema change you shipped.
3. If one `model_version` owns the errors and a canary is in flight:
   `canary_check.sh` auto-rolls back when the candidate error ratio > 1%.
   Manual path: `make rollback`.
4. Check pod health: `kubectl -n inferenceops get pods` for restarts or
   `CrashLoopBackOff`; a crashing model server returns 5xx through the gateway.
5. If errors persist 10 minutes after rolling back the latest change, escalate:
   this is an infrastructure problem (gateway, cluster, dependencies), not a
   model problem.

### HighGpuUtilization: GPU utilisation above 90 %

- **Severity:** info
- **Rule file:** `observability/prometheus/rules/gpu-alerts.yaml`
- **Expression:** `DCGM_FI_DEV_GPU_UTIL > 90` `for: 5m`
- **SLO:** GPU utilization (< 90% sustained)
- **Dashboard:** Grafana `InferenceOps / GPU Utilization` (source `observability/grafana/dashboards/gpu-utilization.json`)

**What it means.** A GPU has run above 90% utilisation for five minutes.
Capacity headroom is gone; latency breaches usually follow. On the CPU-only
kind cluster the DCGM series are absent, so this alert never fires there.

**Response.**

1. Open `InferenceOps / GPU Utilization`. Identify which GPU (`gpu` label) and
   for how long.
2. Correlate with `InferenceOps / Inference Overview`: is request rate up
   (traffic-driven) or is per-request cost up (a heavier model version)?
3. Traffic-driven: let the HPA scale model-server replicas, or raise its max
   replicas if it is already at the ceiling.
4. Model-driven: check `inferenceops_model_info` for a newly loaded version;
   if a heavier candidate is in canary, `make rollback` removes it.
5. Sustained saturation with replicas at max means you need more GPU capacity
   (nodes) or a cheaper serving path (smaller batch, quantized model).

### HighGpuMemoryUsage: GPU framebuffer memory usage above 90 %

- **Severity:** info
- **Rule file:** `observability/prometheus/rules/gpu-alerts.yaml`
- **Expression:** `(DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)) > 0.9` `for: 5m`
- **SLO:** GPU utilization (< 90% sustained)
- **Dashboard:** Grafana `InferenceOps / GPU Utilization` (source `observability/grafana/dashboards/gpu-utilization.json`)

**What it means.** GPU framebuffer memory is above 90% for five minutes. The
risk is a CUDA OOM: the next allocation fails, the serving process crashes,
and `HighErrorRate` follows. Absent on CPU-only nodes, like the utilisation
alert.

**Response.**

1. Open `InferenceOps / GPU Utilization`. Confirm the trend: flat-high (a
   loaded model owns the memory) vs climbing (a leak).
2. Check for OOMKilled restarts already happening:
   `kubectl -n inferenceops describe pod -l app=model-server` (look for
   `Last State: Terminated, Reason: OOMKilled`).
3. Reduce memory pressure: lower the max batch size, unload idle model
   versions, or roll back a canary that loaded a larger artifact
   (`make rollback`).
4. Climbing memory with constant load is a leak: capture `nvidia-smi` output,
   restart the affected pod to buy time, and file a bug with the capture.

## Rollback procedure

Two rollback paths, both converging on `scripts/rollback.sh` (`make rollback`):

**Manual.** `make rollback` is idempotent and safe from any state (canary at
10%, promoted at 100%, or half torn down). It:

1. stops the automated canary check loop (no-op if none is running),
2. deletes the canary Ingress, Deployment, and Service (`--ignore-not-found`),
3. restores the base ingress (`k8s/base/ingress.yaml`): `inferenceops` points
   back at `service-gateway`, reinstating auth/rate-limit,
4. verifies: base ingress backend is `service-gateway` and canary objects are
   gone.

The stable model-server Deployment is never touched; it has been serving the
stable version (1.0.0) the whole time.

**Automated (fail-closed).** During a canary, `scripts/canary_check.sh` runs
every 30s and rolls back via the same `rollback.sh` when:

- candidate p95 > stable p95 x 1.20 (per-version `model_version` queries),
- candidate error ratio > 1%,
- candidate accuracy or f1 drops > 3% vs `evals/reports/baseline.json`,
- Prometheus is unreachable for > 60s, or
- any unexpected error or ambiguous result occurs (fail-closed: ambiguity
  converges to the conservative rollback, never fails open).

Promotion is never automatic. After 3 consecutive healthy checks the script
writes a promote-ready marker and stops; a human runs `make promote`.

**Post-rollback verification gotcha.** The nginx ingress reconciles across
replicas over a few seconds after the ingress change, and terminating canary
pods can still serve the stale route during graceful shutdown. Do not trust a
single probe. Verify with several CONSECUTIVE correct-version responses
(discriminated by the `model_version` response label, e.g. 5 in a row) before
declaring the rollback complete.

## Sample postmortem (blameless): latency regression

This is a worked example in the format every real postmortem should follow.
Blameless means the writeup attacks the system, not people: the goal is to
find the missing guardrail, not the person to punish.

---

### Postmortem 2026-07-15: P95 latency regression from canary 2.0.0 (auto-rolled back)

| Field | Value |
|---|---|
| Incident ID | INC-2026-0715-01 |
| Date | 2026-07-15 |
| Duration of user impact | ~6 minutes (14:04 to 14:10 UTC), 10% of traffic |
| Severity | SEV-3 (latency SLO breach, no errors) |
| Authors | On-call + ML platform |
| Status | Complete |

**Summary.** Canary model version 2.0.0 served predictions roughly 4x slower
than stable 1.0.0. The blended p95 crossed the 500 ms SLO within two minutes of
the traffic shift. The automated canary check detected the per-version
regression and rolled the canary back with no human action. The latency alert
paged on-call after the rollback had already completed.

**Impact.**

- ~10% of `/v1/predict` requests (the canary share) saw p95 ~620 ms against
  the 500 ms SLO for ~6 minutes.
- The latency SLO breached for 2 consecutive 5-minute windows; the error SLO
  was never breached (zero errors throughout).
- Error budget consumed: ~6 minutes of the ~216-minute 30-day budget (~3%).
- The stable 1.0.0 path (90% of traffic) was unaffected the entire time.

**Timeline (UTC).**

- 14:02. `make deploy-canary MODEL_VERSION=2.0.0` starts. The canary check
  loop begins (30s interval).
- 14:04. Canary pod Ready; the 10% traffic shift is live. Candidate p95 climbs
  to ~620 ms within the first minute of traffic.
- 14:06. `canary_check.sh` iteration: candidate p95 0.61s > stable 0.18s x
  1.20. ROLLBACK TRIGGERED. `rollback.sh` removes the canary and restores the
  base ingress.
- 14:07. `HighP95Latency` fires (blended p95 > 500 ms for 2m; the window
  covers the 14:04 to 14:06 impact). On-call acknowledges at 14:09.
- 14:08. Rollback complete and verified: `ingress inferenceops` ->
  `service-gateway`, canary objects gone, 5 consecutive 1.0.0 responses
  observed.
- 14:12. Blended p95 back to ~185 ms as the 5m rate window drains.
  `HighP95Latency` auto-resolves.
- 14:20. Incident closed.

**Root cause.** The candidate artifact replaced the production
`HistGradientBoostingClassifier` with a `GradientBoostingClassifier`
(`n_estimators=500`). The larger ensemble improved golden-dataset accuracy by
0.4 points and passed the eval gate, which validates accuracy and f1 only.
Per-row inference cost rose about 4x. The registration and canary pipeline had
no latency gate, so the slower artifact reached the canary. The canary's
relative p95 check (candidate > stable x 1.20) caught the regression in the
first measurable iteration and rolled it back, exactly as designed.

**Contributing factors.**

- The eval gate measures correctness, not serving cost. A model can pass every
  quality gate and still be unservable at SLO.
- The golden dataset is small, so offline scoring time gave no hint of
  per-row inference cost at production batch sizes.

**What went well.**

- Detection was automatic and fast: ~2 minutes from first bad request to
  rollback trigger, with no human in the loop.
- The blast radius stayed at the 10% canary share; the stable path never
  degraded. This is the canary doing its job.
- The alert and the automation agreed on the numbers (same PromQL, same
  metrics), so there was no confusion about whether the breach was real.

**What went poorly.**

- On-call was paged by `HighP95Latency` at 14:07 for an incident the
  automation had already resolved at 14:06. The page was noise.
- There was no pre-deploy signal at all: the slow artifact passed every gate
  it encountered.

**What we got lucky on.**

- The canary share was 10% and the incident landed in a low-traffic window.
  A 50% shift or peak traffic would have multiplied the budget burn.

**Action items.**

| # | Action | Owner | Type | Status |
|---|---|---|---|---|
| 1 | Add a latency gate to `run_evals.sh`: p95 per-prediction time on the golden dataset, block registration when > baseline x 1.20 | ML platform | Preventive | Open |
| 2 | k6-smoke the candidate artifact in CI before registration (reuse the todo 37 smoke scenario against an ephemeral server) | ML platform | Preventive | Open (depends on todo 37) |
| 3 | Link alerts to this runbook: add a `runbook_url` annotation to every rule in `observability/prometheus/rules/*.yaml` pointing at the matching `#` anchor | Observability | Preventive | Open |
| 4 | Suppress the late page: annotate canary-time alerts with the in-flight canary state so a completed auto-rollback silences the follow-on notification | Observability | Detective | Open |
| 5 | Review this postmortem with the team and schedule action items 1 to 4 | On-call lead | Process | Open |
