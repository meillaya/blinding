# InferenceOps Platform — Learnings

## todo 28 (S3 upload workflow + approval wiring) — 2026-07-22

- **boto3 `upload_file` re-wraps errors outside the botocore hierarchy.** The
  transfer manager catches `ClientError` from the underlying PutObject and
  re-raises `boto3.exceptions.S3UploadFailedError` (a `Boto3Error`, NOT a
  `BotoCoreError`/`ClientError`). A typed S3 upload must catch
  `(Boto3Error, BotoCoreError, ClientError)` or a missing bucket escapes the
  `StorageError` contract. (`download_file` does not wrap — the todo-26 read
  path never hit this.)
- **Workspace members are `package = false`: never installed into the venv.**
  Standalone scripts importing `inferenceops_model_server` need
  `PYTHONPATH=apps/model-server/src` — precedent: Makefile `run-local`
  (`PYTHONPATH=src`) and `apps/model-server/tests/conftest.py` sys.path insert.
  `scripts/upload_model.sh` exports it; basedpyright resolves it via
  `extraPaths` (static only — no runtime effect).
- **`model-eval-gate.yml` runs `make eval` on every PR from a fresh clone** —
  no registry.json exists there. The run_evals.sh approval step therefore
  warn-skips (exit 0) on `ModelNotRegistered` and hard-fails on any other
  approval error; a hard failure on unregistered would break every PR check.
- **StorageError moved s3.py → base.py** when `put_artifact` joined the ABC:
  both backends raise it, so it belongs in the shared module. Kept importable
  from s3.py (`__all__` re-export); constructor `(location, reason)` — the
  attribute renamed `bucket` → `location` (local backend passes its root).
  One test assertion updated (`excinfo.value.location`).
- **Upload ordering invariant:** `put_artifact` writes `model.pkl` FIRST and
  `model_meta.json` LAST — the metadata publishes the artifact to the
  metadata-driven resolution, so a partial upload leaves bytes invisible
  (never a phantom model), and a re-run (idempotent overwrite) completes it.
- **`artifact_dirname(model_id, version)`** is the one-level directory an
  upload lands under (`fraud-detector-1.0.0`); both backends' `_iter_artifacts`
  match exactly one level below root/prefix, so a nested key would be
  unresolvable. Non-`[A-Za-z0-9._-]` chars collapse to `_`.
- **Upload meta fidelity:** the source `model_meta.json` is stored VERBATIM
  (provenance fields like `algorithm`/`feature_names` survive), but its
  recorded identity must match the `(model_id, version)` args — the stored
  metadata IS the resolution key; a mismatch raises `StorageError` instead of
  publishing an artifact that resolves under the wrong identity.
- **s3.py is at 249 pure LOC (warning band).** Cohesive today; if a future
  todo adds lines, split the typed boto3 seam (protocols/TypedDicts/
  `_build_client`/`_resolve_endpoint_url`) into its own module.
- **QA recipe (moto server):** `uv run python -m moto.server -p 5000` (run in
  tmux — a nohup'd server dies when the launching bash tool call times out);
  create the bucket with boto3 + fake static keys; drive `upload_model.sh`
  with `S3_ENDPOINT_URL=http://127.0.0.1:5000 MODEL_BUCKET=...`;
  `REGISTRY_FILE`/`MODEL_CARD_OUT` env knobs keep the QA hermetic (repo's
  registry.json/cards are NOT gitignored). Failure path: dead endpoint
  (`S3_ENDPOINT_URL=http://127.0.0.1:1`) → typed `StorageError` via
  `EndpointConnectionError`.

## todo 31 (model lifecycle end-to-end demo) — 2026-07-22

- **nginx ingress reconcile is a cross-replica RACE — never assert on a single
  post-transition request.** After `make promote` (canary-weight→100) AND after
  `make rollback` (base ingress restored), the nginx data plane reconciles across
  its replicas over a few seconds. A request in that window can hit (a) a
  not-yet-reloaded replica still at the OLD weight, or (b) a TERMINATING canary
  pod still serving the candidate via the stale route during graceful shutdown
  (rollback.sh verifies the Deployment is gone, but pods drain over the
  termination grace period). Both bit this demo: post-promote verify got 1.0.0,
  post-rollback verify got 2.0.0. Fix: a BOUNDED wait for CONSECUTIVE
  correct-version responses (e.g. 5 in a row, ≤40 probes) discriminated by the
  `model_version` response label — consecutive proves every replica reconciled +
  the canary fully drained. `promote.sh`'s own probe only waits for the FIRST
  candidate response, which is necessary but not sufficient for "100% candidate".
- **To trip `regression_gate.py` (not the accuracy gate), the degraded model must
  PASS run_eval.py's accuracy gate (≥0.85) yet drop >3% vs baseline.** An underfit
  `GradientBoostingClassifier(n_estimators=8, max_depth=2, lr=0.1, seed=42)` is
  deterministic and ideal: accuracy 0.9475 (passes ≥0.85) but f1 0.16 → f1 drop
  0.4502 → `REGRESSION DETECTED`, exit 1. A WRONG-SEED GBC drops accuracy to 0.77
  (fails the accuracy gate FIRST — wrong gate). A majority-class predictor
  (DummyClassifier/single stump) gives f1=0 (also trips regression, less nuanced).
  The degraded model is written to `fraud_detector/model.pkl` (the path run_eval.py
  scores), backing up + restoring the good artifact around it.
- **`run_evals.sh` hardcodes approving the version in `fraud_detector/model_meta.json`
  (1.0.0)** — it evaluates the stable artifact path (eval_common.MODEL_PATH) and
  approves that version label. The candidate (2.0.0) is therefore approved via a
  DIRECT `approve_model.py --version 2.0.0` call (the SAME CLI run_evals.sh uses),
  narrated as such. The eval gate validates the WEIGHTS; the model-version is an
  opaque registry label decoupled from both the weights and git semver.
- **moto in a self-contained script:** `setsid uv run python -m moto.server -p <port>
  &`; `$!` is the setsid'd process — kill via process group (`kill -- -PID`) PLUS a
  `pkill -f "moto[.]server"` char-class backstop (uv may spawn a python child).
  Readiness = poll `curl $endpoint`. Use a DEDICATED port (5011) to avoid clashes
  with a stray moto on 5000. The bucket MUST be created (boto3 `create_bucket`)
  before `upload_model.sh` — a missing bucket → `S3UploadFailedError`.
- **The candidate's model_version-filtered p95 is only measurable while traffic
  flows** (canary_check.sh's 1m rate window). A background curl loop (~5 req/s via
  `sleep 0.2`, `setsid` + a sentinel `$0` arg so `pkill -f "...Lo[o]p"` cleans it
  up) against `localhost:8080/v1/predict` with the Host header ONLY (gateway
  bypassed during the canary). Without it, canary_check.sh reports "CANARY WARMING
  UP" (bounded 240s grace) and NEVER reaches promote-ready.
- **The live gateway API key is in the pod env, not necessarily the example secret:**
  `kubectl exec deploy/gateway -- printenv API_KEYS` (here `test-key`, while
  secret.example.yaml shows the REPLACE-ME values). Read it live for any post-
  rollback verification through the reinstated gateway.
- **Output capture under `set -euo pipefail`:** `exec > >(tee -a "$qa") 2>&1` with
  fd save/restore (`exec 3>&1 4>&2` … `exec >&3 2>&4`) + `"$fn" || rc=$?` keeps the
  phase in the CURRENT shell (set -e + the EXIT trap behave normally — no pipeline
  subshell) while tee'ing all output (echo + external commands) to the QA file;
  restoring the fds before returning lets tee flush even when the phase failed.
- **Idempotent EXIT trap = safe failures.** The demo's trap (stop traffic/moto via
  pidfile + char-class pgrep backstop, `scripts/rollback.sh` (idempotent), restore
  the stable artifact from a backup, remove temp registry/card + eval-reports via a
  before/after snapshot) left the cluster + repo CLEAN even when the script failed
  mid-run — verified across two failed runs before the reconcile fix.
- **This dev tmux runs fish, not bash** — `${PIPESTATUS[0]}` etc. get parsed by fish
  even inside quotes; drive bash work via a runner SCRIPT FILE, not inline commands.

## CLUSTER RECOVERY + GATEWAY KEYS (orchestrator) — 2026-07-22
- **Host swap-thrash incident (resolved):** during todo-31 verification the kind cluster went FULLY unresponsive (API + data plane + docker stats dead). Root cause was HOST memory exhaustion (5+ concurrent opencode agent sessions ~0.6-1GB each + full kind stack + clickhouse on 62GiB RAM) -> 33.8GiB swapped, 96 D-state procs, I/O wait 32-38%. `docker kill -s KILL`/`docker restart` BOTH timed out (SIGKILL cannot interrupt uninterruptible D-state I/O wait). **Resolution:** user freed memory (closed idle sessions) -> D-state cleared (96->1), kind node SELF-RECOVERED (kubelet/apiserver came back without a restart). LESSON: if the cluster hangs, check `vmstat`/`free -h`/D-state count FIRST; the fix is reducing host memory pressure, NOT forcing the container.
- **Gateway API keys (REAL values):** the deployed gateway accepts `dev-key-REPLACE-ME-0000000000000000` and `dev-key-REPLACE-ME-1111111111111111` (from Secret `gateway-api-keys`, key `API_KEYS`, CSV) — NOT `test-key`. Use these for any gateway-authenticated curl (`-H 'X-API-Key: dev-key-REPLACE-ME-0000000000000000'`). `/healthz` is open (no key).
- **Cluster verified healthy post-recovery:** model-server (2) + gateway (2) Running; model-server serves model_version=1.0.0 (/healthz,/readyz 200, /v1/predict valid); Prometheus `prometheus-kube-prometheus-kube-prome-prometheus-0` 2/2 + Grafana 3/3 Running; ingress-nginx controller Running; base ingress -> service-gateway; HPA model-server min2/max10 at 2 replicas.
- **Prometheus query from host:** `kubectl -n monitoring port-forward svc/kube-prometheus-kube-prome-prometheus <local>:9090` then `/api/v1/query`. (Right after a pod restart, inferenceops_* metrics may be absent until traffic flows + a scrape interval passes.)
- **W7 cluster-awareness:** the cluster just recovered — W7 workers MUST be cluster-stable-aware: bounded helm/kubectl ops, do NOT leave extra replicas/orphan pods, clean up port-forwards, and prefer the grafana SIDECAR ConfigMap (label `grafana_dashboard: "1"`) for dashboards over a second concurrent `helm upgrade kube-prometheus` (todo 32 already upgrades that release — concurrent upgrades to the SAME release conflict).

## todo 34 (OTel collector + trace wiring + Loki) — 2026-07-22

- **`kind load` is BROKEN on this cluster node: `unknown containerd config version: 4 (supported: 2 and 3)`** — the installed kind predates containerd 2.3's config v4. Workaround that works: `docker save <img> | docker exec -i inferenceops-control-plane ctr -n k8s.io images import -` (stdin pipe; `docker cp` to the kind node SILENTLY fails under rootless docker — the file never arrives). ALSO: `kind load` skips images whose TAG already exists on the node (checks tag, not digest), so a rebuilt `:dev` image is never updated by it — the `ctr import` path replaces by digest and is the only reliable reload. Verify with `docker exec <node> ctr -n k8s.io images ls | grep <repo>` digest == local `docker image inspect --format '{{.Id}}'`.
- **`kubectl set env` poisons the env entry for `apply -k`:** `set env VAR=literal` writes a plain `value:`; a later `kubectl apply -k` that declares the SAME env var as `valueFrom:` is REJECTED by the API (`may not be specified when 'value' is not empty`) — the strategic-merge patch can't cross the value/valueFrom boundary. Restore path: `kubectl set env deploy/X VAR-` (remove) THEN `apply -k`. (Bit the todo-34 failure-QA restore.)
- **Rollout-aware failure measurement:** after swapping a Deployment's OTEL_ENDPOINT to a dead one, the first ~15-20s still show spans arriving — the OLD pods' shutdown flush (preStop drain + BatchSpanProcessor export-on-shutdown) lands in that window. Assert "0 new spans" only after a ~20s settle with no traffic, then open the measurement window.
- **OTel timing constants for QA waits:** gateway/model-server BatchSpanProcessor default schedule delay = 5s; collector `batch` timeout = 5s → wait ~12s after traffic before asserting `otelcol_receiver_accepted_spans` grew. OTLP METRIC export (PeriodicExportingMetricReader) defaults to a 60s interval → the collector's prometheus exporter (:8889) shows app metrics only after ~60s of gateway uptime.
- **Collector 0.102.x confmap has NO env-default syntax:** `${VAR:-default}` → "invalid uri", `${env:VAR:-default}` → "invalid name" (the env provider rejects `:-`). Hardcode defaults in collector.yaml; validate configs with `docker run --rm -v $PWD/collector.yaml:/etc/otelcol/config.yaml:ro otel/opentelemetry-collector:0.102.1 validate --config=/etc/otelcol/config.yaml` (exit 0 + no output = valid). The `logging` exporter still exists in 0.102.1 (removed in newer majors → that's why the image is pinned).
- **Collector span-reception evidence, three ways:** (1) internal self-metrics on :8888 → `otelcol_receiver_accepted_spans{receiver="otlp"}` (THE counter for the failure QA — it has no per-service label, so misconfigure ALL exporting apps to get it to 0); (2) `logging` exporter stdout lines `TracesExporter {"resource spans": N, ...}` where N = distinct service instances (4 = 2 gateway + 2 model-server pods); (3) :8889 prometheus exporter re-exposes OTLP metrics (`gateway_requests_total` with `job="inferenceops-gateway"`). `service.telemetry.metrics.address` MUST be 0.0.0.0:8888 (default localhost is unreachable via kubectl port-forward, which dials the pod IP).
- **Compose override files APPEND sequence keys (ports!)** — an override `ports: ["28080:8080"]` ADDS to the base's "8080:8080", it doesn't replace. Use the `!override` tag (compose ≥ v2.24; this host runs v5.3): `ports: !override ["28080:8080"]`. Host :8080 is owned by kind's ingress extraPortMappings and :18080 was squat by another session → QA used :28080.
- **loki-stack 2.10.3 (app v2.9.3) on kind:** `helm upgrade --install loki grafana/loki-stack --version 2.10.3 -n monitoring -f observability/loki/values.yaml`. Grafana subchart is DISABLED by default (no values needed to avoid the second Grafana). Promtail's DEFAULT scrapeConfig tails /var/log/containers/*.log with kubernetes_sd relabeling → `{namespace="inferenceops", pod=~"gateway-.*"}` queries work with ZERO custom scrape config; uvicorn access logs (`"POST /v1/predict HTTP/1.1" 200 OK`) are the per-request evidence lines. QA via `kubectl -n monitoring port-forward svc/loki 23100:3100` + `/loki/api/v1/query_range` (logcli not installed on this host).
- **Model-server telemetry is traces-ONLY (todo 34):** its metrics story stays Prometheus text exposition in metrics.py (todo 35's domain). `telemetry.py` mirrors the gateway's todo-11 design (init_telemetry + pure-ASGI TelemetryMiddleware, OTLP/HTTP iff OTEL_ENDPOINT, log-and-drop on unreachable collector) minus the meter. Test file MUST have a unique basename across apps — tests dirs have no `__init__.py` and one pytest session collects both apps, so gateway's test_telemetry.py forced the model-server file to `test_tracing.py` (collision = "import file mismatch" collection error).
- **Repo pytest arg oddity (pre-existing, NOT todo-34's):** `uv run pytest <path> --flags` (path BEFORE flags) collects the WHOLE suite (224 tests, testpaths applied, path silently ignored); `uv run pytest --flags <path>` (flags first) collects just the path. Run single files flags-first.

## todo 35 (ML/drift metrics + worker drift_job) — 2026-07-22

- **PSI is exactly 0.0 on identical data even with epsilon clipping.** The
  clip-to-ε (1e-6) on bin shares preserves EQUALITY of identical distributions,
  so golden-vs-golden scores a literal 0.0 — a clean deterministic in-distribution
  baseline for tests/QA. A 3σ-per-column shift of all 20 golden features scores
  mean-PSI 7.42 (threshold 0.2); two independent same-seed N(0,1) draws of 400
  rows score <0.05 with every per-feature KS <0.2.
- **drift_job reuses the model-server's `MetricsRegistry` cross-app** (single
  source of truth for `inferenceops_drift_score`): the worker tests' conftest
  inserts BOTH `apps/worker/src` AND `apps/model-server/src` into sys.path; the
  CLI needs `PYTHONPATH=apps/worker/src:apps/model-server/src`. basedpyright
  resolves both via the existing `extraPaths` — zero suppressions needed.
- **ruff has no known-first-party config**, so `inferenceops_model_server` sorts
  ALPHABETICALLY among third-party imports (between `numpy` and `numpy.typing`).
  Run `ruff check --fix` rather than hand-ordering cross-app imports.
- **Prediction-label histogram buckets `(0.5, 1.0)`**: cumulative semantics —
  `le="0.5"` counts label-0 predictions, `le="1.0"` counts all binary labels.
  Confidence buckets refine [0,1] near the extremes (0.05…0.95, 0.99, 1.0).
  `inferenceops_model_info` uses labels `model_id`/`version` (NOT `model_version`
  — spec §M7 names them differently); `inferenceops_drift_score` is UNLABELED
  (the alert threshold is global), rendering as a bare `name value` line.
- **drift_job CLI exit-code contract:** 0 = stable, 1 = drift alert (DRIFT ALERT
  on stderr), 2 = operational error (bad/missing JSONL) — mirrors approve_model.py's
  typed-error→stderr→nonzero pattern; the repo has NO logging framework, so
  stderr print IS the alert channel.
- **QA stayed fully local (cluster-stable-aware):** uvicorn subprocess on :8199
  with `PYTHONPATH=apps/model-server/src MODELS_DIR=models/artifacts`, 15 scored
  rows (10 predict + 1 batch-of-5) → both histograms count=15.0 + model_info=1 in
  `/metrics`; zero kubectl/helm ops, EXIT-trap cleanup killed the server. The
  server's own `/metrics` shows `inferenceops_drift_score 0.0` (gauge default) —
  the worker sets it in its OWN registry / textfile-collector file, not the server's.

## todo 33 (Grafana dashboards — single JSON source, two wrappers) — 2026-07-22

- **Kind Grafana admin password is NOT the chart default `prom-operator`.** The
  deployed `kube-prometheus-grafana` secret holds a GENERATED 44-char password:
  `kubectl -n monitoring get secret kube-prometheus-grafana -o jsonpath='{.data.admin-password}' | base64 -d`
  (user `admin`). Read it live for any `/api/*` call; guessing `prom-operator`
  returns an auth-error object that breaks `jq` pipelines silently-looking.
- **The sidecar path is fully declarative + self-healing.** A ConfigMap labeled
  `grafana_dashboard: "1"` (ns ANY — the sidecar runs `NAMESPACE=ALL`) is
  hot-loaded by `grafana-sc-dashboard` within ~3-6s (inotify watch, not a poll).
  When todo 32's PARALLEL `helm upgrade` rolled the grafana pod mid-verification
  (new ReplicaSet), all 4 dashboards re-provisioned automatically from the
  persistent ConfigMap — no re-apply needed. This is why the sidecar path beats
  a second concurrent helm upgrade: it survives release churn it does not own.
- **kustomize load-restrictor forces the wrapper to live beside the source.**
  `kubectl apply -k` uses `LoadRestrictionsRootOnly`: a `configMapGenerator`
  cannot reference `../dashboards/*.json` from a sibling dir ("security: file is
  not in or below"). So the kind wrapper `kustomization.yaml` sits IN
  `observability/grafana/dashboards/` next to the 4 JSONs — the single source IS
  the kustomization root. Harmless to compose: the Grafana file provider only
  parses `*.json` and ignores the yaml. Apply:
  `kubectl --context kind-inferenceops apply -k observability/grafana/dashboards`.
  Use `labels:` (kustomize v5, bundled kubectl v1.36) with `includeSelectors:
  false`, NOT deprecated `commonLabels`; `disableNameSuffixHash: true` keeps the
  name stable (`inferenceops-grafana-dashboards`) so re-applies are updates, not
  delete+recreate.
- **Datasource-mismatch failure is a clean HTTP 404, not a panel-level error.**
  `/api/ds/query` with a bogus datasource uid returns `404 {"message":"Data
  source not found"}` — the same expr with uid `prometheus` returns 200. That is
  the exact "no data" mode the acceptance guards: bind BOTH the panel-level AND
  every target-level `datasource` to `{"type":"prometheus","uid":"prometheus"}`
  (verify with `jq '[.panels[].datasource.uid, .panels[].targets[].datasource.uid]
  | unique'` → must be `["prometheus"]`).
- **Panels can be valid-but-empty by design.** No GPU on host (DCGM_* panels),
  no kube-state-metrics on compose (replicas panel), todo-35 metrics not shipped
  yet (model-quality: `inferenceops_prediction_distribution`,
  `inferenceops_confidence_distribution`, `inferenceops_drift_score`,
  `inferenceops_model_info`, plus `inferenceops_eval_accuracy` for the golden-
  dataset accuracy panel and `inferenceops_queue_wait_seconds` for the latency
  queue stage, `inferenceops_tokens_total` for LLM tokens/sec). The EXPRESSIONS
  are correct; the series appear when the exporter/metric lands. schemaVersion 39
  renders on both the kube-prometheus-stack Grafana (11.x) and compose latest.
- **Compose verification needs only `docker compose up -d prometheus grafana`**
  (no app build) — provisioning is independent of the app services. admin/admin
  (GF_SECURITY_ADMIN_PASSWORD). `/api/ds/query` returns 200 even with zero app
  metrics (empty frame). `docker compose down` afterwards restores the prior
  not-running state.
- **QA temp-artifact discipline:** the failure-path dashboard rode a TEMP
  ConfigMap (`t33-bad-ds-test`, same `grafana_dashboard: "1"` label), deleted
  with `--wait=true` and confirmed ABSENT from `/api/search` before finishing —
  only the deliverable `inferenceops-grafana-dashboards` ConfigMap remains in
  `monitoring`. Port-forwards killed by saved PID; `pgrep -af port-forward` to
  confirm no orphans.

## todo 32 (Prometheus alert rules + values.yaml) — 2026-07-22

- **todo 19 never created `observability/prometheus/values.yaml`** — it installed
  kube-prometheus-stack with chart DEFAULTS (`helm get values` → `null`). The
  deploy script's comment ("If a values file is present we use it") was forward-
  looking. todo 32 CREATED the file (not extended a non-existent one), making the
  implicit defaults explicit (grafana sidecar label/uid, serviceMonitorSelector-
  NilUsesHelmValues) and adding `additionalPrometheusRulesMap`.
- **`additionalPrometheusRulesMap` creates PrometheusRule CRs named
  `<fullname>-<key>`** (here `kube-prometheus-kube-prome-inferenceops-*-alerts`).
  The map value is the `spec` of the PrometheusRule (i.e. `groups: [...]`), NOT
  the full CR. The standalone `rules/*.yaml` files are the authoritative reference
  (full CR shape for direct `kubectl apply`); the values.yaml embeds the same
  groups for Helm-managed lifecycle.
- **Synthetic breach approach:** `kubectl patch` the PrometheusRule CR to set
  HighP95Latency expr → `vector(1) > 0`, for → `0s`. Prometheus fires it within
  ~15 s (one evaluation cycle). Restore with a SECOND `kubectl patch` back to the
  real expr — NOT `helm upgrade`: helm's 3-way merge does NOT revert out-of-band
  CR edits when old==new manifest (the values.yaml didn't change between upgrades).
  Always patch back manually after a synthetic-breach test.
- **prometheus-operator CRD validating webhook rejects malformed PromQL at
  ADMISSION time** — `kubectl patch` with a syntactically invalid expr returns
  `The "prometheusrules" is invalid: ... could not parse expression: parse error:
  unexpected end of input` and the CR is NOT updated. This is stronger than a
  Prometheus rule-load error: the broken rule never reaches the cluster. (If it
  did bypass admission, Prometheus reports it in `/api/v1/rules` via `lastError`
  with `health=err`.)
- **GPU alerts (DCGM_FI_DEV_*) are valid PromQL with no GPU present** — the
  series are simply absent, so the expressions evaluate to no result and the
  alerts stay `inactive` forever. No special handling needed; the PromQL loads
  cleanly (health=ok, state=inactive, lastError='').
- **ServiceMonitor selection:** the model-server ServiceMonitors carry
  `release: kube-prometheus` (the Helm release label). With
  `serviceMonitorSelectorNilUsesHelmValues: true` (chart default, made explicit
  in values.yaml), the Prometheus CR selects them. No `serviceMonitorSelector: {}`
  override needed.
