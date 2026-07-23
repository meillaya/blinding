# inferenceops-platform - Work Plan

## TL;DR (For humans)
**What you'll get** — *InferenceOps*, a production-grade ML/LLM serving platform on AWS EKS + local `kind`, built as a flagship portfolio project. **50 dependency-ordered todos across 10 waves / 6 independently-verifiable components:** a `devenv.sh` reproducible dev env (W1); a FastAPI **fraud-detection model server** + API gateway with auth/rate-limit/OTel (W2–W3); Docker images + **GitHub Actions CI with golden-dataset eval gates** (W3); a **`kind` Kubernetes deployment** with probes/resources/PDB/rolling-update/graceful-shutdown and **HPA + KEDA autoscaling** (W4); **Terraform AWS modules** — VPC/EKS/ECR/S3/IAM(IRSA)/ALB/monitoring, validated *offline* (W5); a **model registry + nginx 90/10 canary + automated rollback** (W6); **Prometheus/Grafana/OTel/Loki** observability with 4 dashboards, alert rules, drift metrics, and an incident runbook (W7); **k6 + Locust load tests + a real benchmark table** (W8); **vLLM (GPU) + KServe backends + a CPU-runnable llama.cpp/ollama LLM path** (W9); and **staff-grade docs/README + interview deep-dives + a failure-scenario suite** (W10). The headline workflow: **deploy → eval gate → 90/10 canary → monitor latency/errors/quality → auto-rollback on regression; promotion is a deliberate gated step (`make promote`) once the canary observation window is clean.**

**Why this approach** — *Local-first + AWS-IaC-ready*: the entire system is verifiable on this machine with **no AWS spend and no GPU**, while the Terraform / Karpenter / GPU-node-group paths are real and EKS-ready. `devenv.sh` gives a reproducible toolchain; phasing follows your Phase 1→4 sequence; Phase 1 (W1–W4) is a complete, demoable deliverable on its own.

**What it will NOT do** — no training models from scratch (an artifact script only); no real production traffic and **no automated `terraform apply`** (apply stays manual); no Istio/service-mesh; **no Ray Serve implementation** (scaffold + docs only); no notebook demos; **no scope reduction** (the full request is planned — phasing is ordering, not a subset).

**Effort** — Large: 50 todos, ~10 waves, ~100+ files. Phase 1 (W1–W4) is the minimum viable, independently-runnable milestone; full completion is all 10 waves plus the F1–F4 verification wave.

**Risk** — Highest-risk items, each already mitigated in the plan: `kind` under **rootless docker** (de-risked — `DOCKER_HOST=unix:///run/user/1000/docker-me.sock` is wired into devenv and prerequisites were verified present); the in-cluster **observability↔autoscaling dependency chain** (resolved by moving Prometheus bootstrap from W7 into **W4** so the custom-RPS HPA and canary rollback have metrics); **KServe install weight** on a single node (capped to Standard-mode install + a trivial InferenceService `Ready` check, no Istio). **vLLM/GPU serving and GPU-pressure / multi-node failure scenarios are documented, not runtime-verified** (no GPU on host).

**Decisions locked at approval** — AWS = *local-first + IaC-ready* (offline `terraform validate` for all envs + offline `terraform plan` against a local **moto** mock endpoint with three EKS offline guards — `use_latest_ami_release_version=false`, `addons` omitted, `enable_irsa` off; real `apply` + IRSA OIDC need real credentials); LLM = *CPU-runnable + GPU-ready* (llama.cpp/ollama locally, vLLM as the GPU build/doc path); Tests = *tests-per-todo* with coverage gates (model-server ≥80% / gateway ≥70%) + eval gates. **Metis-driven structural fixes baked in:** rootless `DOCKER_HOST`; Prometheus in W4; `kind load` + `imagePullPolicy: IfNotPresent/Never`; offline Terraform provider; Karpenter treated as EKS-only. **Librarian-verified contracts baked in:** devenv `devenv.yaml`/`devenv.nix` split (`kubernetes-helm`, terraform-unfree); EKS v21 (`name`/`kubernetes_version`/`addons`); IAM v6 (`iam-role` for IRSA); KServe `Standard` mode (canary is Serverless-only on 0.18); KEDA prometheus `query`+`threshold`; vLLM v0.25.1 `vllm serve`; kube-prometheus-stack 87.19.0 + nginx canary annotations.

## Scope

**IN — the full request, all 4 phases / 9 milestones, dependency-ordered (no scope reduction):**
- **C1 Dev env & scaffold:** devenv.sh (flakes.nix) toolchain, full repo tree (spec §3), uv workspace, self-documenting Makefile, package skeletons.
- **C2 Local inference + Docker + CI (M1–2):** FastAPI model server (fraud-detection classifier), API gateway (auth / token-bucket rate-limit / OTel), multi-stage Docker images, docker-compose stack, GitHub Actions CI (ruff / basedpyright / pytest+coverage / integration / docker build / Trivy scan / ECR push gated / eval gate), golden-dataset evals + latency + regression gates.
- **C3 Kubernetes + autoscaling (M3, M6):** `k8s/base` + overlays (local/staging/prod) via kustomize, kind local cluster, probes / resources / PDB / rolling-update / graceful-shutdown / anti-affinity / taints-tolerations, HPA (CPU + memory + custom RPS via prometheus-adapter), KEDA ScaledObject, Karpenter (Terraform ref).
- **C4 AWS infrastructure IaC (M4):** Terraform modules vpc / eks / ecr / s3 / iam / alb / monitoring + envs dev/staging/prod + infra scripts (bootstrap, GPU plugin, prometheus install, destroy).
- **C5 MLOps lifecycle + canary/rollback (M5, M8):** storage abstraction (Local/S3 behind one interface), model registry (register / promote / metadata JSON / model-card), S3 upload workflow, nginx-ingress canary (90/10), **automated rollback** (p95 +20% / errors >1% / eval −3% / GPU-mem thresholds + Prometheus-unavailable conservative fallback after 60s, **fail-closed on any check error**, driven on a **30s interval**); **promotion stays a deliberate gated step** (`make promote`) once the canary observation window is clean.
- **C6 Observability + load testing + advanced serving + docs (M7, M9, Phase 4):** kube-prometheus-stack + 4 Grafana dashboards + alert rules + OTel collector + Loki, ML/drift metrics + worker batch jobs, incident runbook + SLOs/SLIs, k6 (smoke/spike/soak/stress) + Locust + benchmark report generator, vLLM (GPU) + KServe backends + ollama/llama.cpp CPU LLM stand-in, full docs/README + interview deep-dives + failure-scenario suite.

**OUT / Must-NOT-Have (guardrails, NOT scope cuts):**
- No training models from scratch — `models/training/train.py` produces a trained fraud-detection artifact from a public/synthetic dataset; the platform *serves* artifacts, it does not research models.
- No real production traffic; no automated `terraform apply` — apply is manual/user-triggered; CI deploy jobs are demonstrated against kind. *(default; user may override to real AWS)*
- No Istio / service-mesh — canary is nginx-ingress annotation-based only.
- No Ray Serve implementation — spec-optional → scaffold + documentation only.
- No notebook demos, no GUI, no multi-tenant auth beyond simple API-key.
- No scope reduction: the plan covers the entire request; phasing is execution ordering, never a subset.

**Defaults locked at approval (user replied "approve"):**
- **Q1 AWS = Local-first + IaC-ready** (no spend / no credentials required; offline `terraform validate` for all envs + offline `terraform plan` against a local moto mock endpoint with three EKS offline guards — `use_latest_ami_release_version=false`, `addons` omitted, `enable_irsa` off; real `apply` is manual).
- **Q2 LLM/GPU = CPU-runnable + GPU-ready** (vLLM/KServe fully built; local LLM demo via ollama/llama.cpp on CPU; GPU benchmarks only where hardware exists).
- **Q3 Tests = Tests-per-todo + gates** (impl+test atomic; pytest via uv; coverage model-server ≥80% / gateway ≥70%; golden-dataset eval gates; k6 SLO thresholds; canary regression tests; agent-executed happy+failure QA on every todo).

## Verification strategy
- **Locked test strategy = tests-per-todo:** every implementation todo bundles its pytest tests (Implementation + Test = ONE todo). Python tests run via `uv run pytest`. Coverage gates (model-server ≥80% / gateway ≥70%) enforced in CI. **Agent-executed QA on EVERY todo** — a happy path AND a failure path, each with a concrete evidence path (exact command + artifact + assertion). Zero human-intervention verification.
- **Verification layers (bottom-up):** unit (pytest) → integration (FastAPI `TestClient` / docker-compose) → cluster (kind + kustomize) → IaC (`terraform validate` + `terraform plan`) → eval gates (golden dataset / latency / regression) → load (k6 / Locust with SLO thresholds) → canary regression (synthetic bad metrics trigger rollback).
- **Per-component exit checks (each independently verifiable — Components ledger in `.omo/drafts/inferenceops-platform.md`):**
  - **C1:** `devenv shell` provides the full toolchain; `make help` lists every target; `uv sync` + a smoke pytest pass.
  - **C2:** `make run-local` → `curl :8080/v1/predict` returns valid JSON; `/healthz` 200, `/readyz` 503-before/200-after model load; `/metrics` Prometheus text format; `make test` + `make ci-local` pass; eval gate blocks an injected regression.
  - **C3:** `make deploy-local` → all pods `Running` on kind; probes pass; `make load-test-spike` → HPA scales 2→5+ then scales down.
  - **C4:** `terraform validate` succeeds offline for dev/staging/prod; `terraform plan` succeeds offline against the local moto mock endpoint (todo 24) with the three EKS offline guards (`use_latest_ami_release_version=false`, `addons` omitted, `enable_irsa` off); infra scripts are shellcheck-clean.
  - **C5:** registry register/promote round-trip works; storage backends tested (Local real, S3 mocked via moto); canary routes ~10% to candidate (discriminated by a `model_version` metric label); a synthetic p95/eval regression triggers automatic rollback; a metrics outage **or any check error** triggers conservative (fail-closed) rollback; a clean observation window reports promote-ready for the deliberate `make promote`.
  - **C6:** Grafana dashboards render (provisioned via compose); an alert fires on a synthetic breach; traces→OTel collector and logs→Loki; all 4 k6 scenarios run + a benchmark table is populated with real local numbers; vLLM/KServe manifests build; local `/v1/completions` works via llama.cpp on CPU; docs/README complete and quickstart commands work from a clean checkout.
- **Misleading-success guard:** passing logs, subagent summaries, and grep hits are *claims*; every QA step records the exact command, the artifact produced, and the assertion that ran (its evidence path). No done-claim without that triple.

## Execution strategy
- **One plan, dependency-ordered waves.** Each todo is independently committable; a wave's exit check must pass before the next wave begins. Implementation + Test = ONE todo. Target 5–8 todos per wave.
- **Parallelism:** within a wave, todos touching disjoint files may run in parallel (model-server vs gateway in C2; vpc/ecr vs eks/iam in C4; dashboards vs alert-rules vs OTel in C6). Cross-wave dependencies are strictly serial.
- **Local-first invariant:** everything is verifiable without AWS or GPU. AWS is validated via `terraform plan` only. The GPU path is fully built + documented; the LLM path is demoed locally on CPU via ollama/llama.cpp.
- **Wave map (full detail in `## Todos`):**
  - **W1 — C1 foundation:** repo tree, devenv, uv workspace, Makefile, package skeletons.
  - **W2 — C2a fraud model + model server:** dataset/train/eval → artifact; loader/predictor/schemas; batching/metrics; FastAPI server; Dockerfile.
  - **W3 — C2b gateway + compose + CI + evals:** gateway; docker-compose; golden-dataset evals + gates; CI workflows; coverage gate.
  - **W4 — C3 k8s + autoscaling:** base manifests; ingress/HPA/PDB; overlays; kind deploy script; KEDA/Karpenter/prometheus-adapter.
  - **W5 — C4 Terraform:** vpc+ecr; eks+iam; s3+alb+monitoring; envs; infra scripts.
  - **W6 — C5 registry + canary + rollback:** storage abstraction; registry; upload workflow; canary manifests; automated rollback.
  - **W7 — C6a observability:** prometheus+rules; grafana dashboards; OTel+Loki; ML/drift metrics + worker; runbook+SLOs.
  - **W8 — C6b load testing:** k6 smoke/stress; k6 spike/soak; locust; benchmark report generator.
  - **W9 — C6c advanced serving:** worker batch; vLLM; CPU-LLM via llama.cpp; KServe (+Ray scaffold); serving docs.
  - **W10 — docs + polish:** README; architecture/aws/k8s docs; autoscaling/observability/cost docs; security/tradeoffs/interview docs; failure-scenario suite.
- **Final verification wave (F1–F4)** runs in parallel after ALL todos; ALL four must APPROVE before completion is declared, then results are surfaced and the user's explicit okay is awaited.

## Todos
> **How to read:** each todo is ONE atomic, independently-committable unit (Implementation **+ its tests**). Every todo carries **References** (the executor has NO interview context — these are exhaustive), agent-executable **Acceptance** criteria, a **QA happy** + a **QA failure** scenario (each with an evidence path), and a **Commit** line. Waves are dependency-ordered; a wave's exit check (see `## Verification strategy`) must pass before the next wave starts.
>
> **Environment invariants (from the mandatory Metis gap-analysis, verified on this host):**
> - Docker is **rootless** on a custom socket → every docker/kind/compose/act/trivy command runs with `DOCKER_HOST=unix:///run/user/1000/docker-me.sock` (exported by devenv `enterShell`, todo 2).
> - **In-cluster Prometheus is bootstrapped in W4 (todo 19)**, NOT W7 — the custom-RPS HPA (todo 20) and canary auto-rollback (todo 30) depend on it.
> - **No registry** → images are built locally and `kind load docker-image`'d (todo 19); workloads use `imagePullPolicy: IfNotPresent/Never`.
> - **No AWS credentials on host** → Terraform is verified offline via `terraform validate` (all envs) + `terraform plan` against a local **moto** mock endpoint with EKS offline guards (three EKS offline guards: `use_latest_ami_release_version=false` so no SSM AMI read, `addons` omitted offline, `enable_irsa` off) — see todo 24; real `apply` + IRSA OIDC need real credentials and stay manual.
> - **"spec §X"** refers to the user's original project specification; the operative content of each cited section is embedded inline in the referencing todo (e.g., the §3 tree in todo 1, the §4 API in todo 9, the §6 README sections in todo 46), so the plan is self-contained for execution and for the F1/F4 fidelity audit.
> - **No GPU** → vLLM is build/doc-verified only (todo 42); the LLM path is demoed on CPU via llama.cpp/ollama (todo 43); GPU-pressure + multi-node failure scenarios are documented, not executed (todo 49).
> - **Karpenter is EKS-only** (no cloud provider on kind) → documented as a Terraform reference (todos 20, 23), never expected to scale nodes on kind.

### Wave 1 — C1: Dev environment & scaffold (exit: `devenv shell` gives full toolchain; `make help` works; smoke pytest passes)

- [x] 1. Initialize repo tree, .gitignore, README stub, LICENSE
  - **References:** spec §3 full directory tree; `.omo/drafts/inferenceops-platform.md` (Must-NOT-Have). Create every dir from spec §3: `apps/{gateway,model-server,worker}/{src,tests}`, `models/{training,registry,artifacts}`, `serving/{fastapi,kserve,ray-serve,vllm}`, `infra/{terraform/{envs/{dev,staging,prod},modules/{vpc,eks,ecr,s3,iam,alb,monitoring}},scripts}`, `k8s/{base,overlays/{local,staging,prod}}`, `observability/{prometheus/rules,grafana/{dashboards,datasources},opentelemetry,loki}`, `load_tests/{k6,locust}`, `evals/reports`, `docs`, `scripts`. Use `.gitkeep` for empty dirs.
  - **Acceptance:** `git init` done; `find . -type d` matches the spec §3 tree; `.gitignore` covers `.env`, `*.tfstate*`, `.terraform/`, `__pycache__/`, `*.pyc`, `.venv/`, `.coverage`, `htmlcov/`, `.devenv*/`, `models/artifacts/**/*.pkl` (keep their README), `dist/`; `devenv.lock` is NOT ignored (commit it); README.md stub + MIT LICENSE present.
  - **QA happy:** `find . -type d | sort > /tmp/qa/t1_tree.txt`; diff against expected tree → match.
  - **QA failure:** create `.env` and `__pycache__/x.pyc`; `git status --porcelain > /tmp/qa/t1_ignore.txt` shows neither tracked.
  - **Commit:** `scaffold(repo): initialize repo tree, .gitignore, README stub, LICENSE (todo 1)`

- [x] 2. devenv.yaml + devenv.nix + .env.example (full toolchain + rootless DOCKER_HOST)
  - **References:** devenv splits config — `devenv.yaml` holds ONLY `inputs`/`nixpkgs.*`; `packages`/`languages`/`processes`/`containers` go in `devenv.nix` (https://devenv.sh/reference/yaml-options/, https://devenv.sh/reference/options/). Exact nixpkgs attrs: `kind`, `kubectl`, `terraform` (BUSL/unfree ≥1.6 → needs `nixpkgs.allow_unfree: true`), `k6`, `kubernetes-helm` (**NOT** `helm`), `trivy`, `docker`, `shellcheck`, `actionlint`, `ast-grep`, `jq`, `act`, `uv`, `kustomize`, `hadolint`. Python 3.12 via `languages.python.version="3.12"` + `inputs.nixpkgs-python` (https://devenv.sh/languages/python/). Pin `inputs.nixpkgs.url: github:NixOS/nixpkgs?ref=nixos-25.05`. **Metis:** docker is rootless on `unix:///run/user/1000/docker-me.sock` → `enterShell` must `export DOCKER_HOST=unix:///run/user/1000/docker-me.sock` and `export KIND_EXPERIMENTAL_PROVIDER=docker`.
  - **Acceptance:** `devenv shell` enters env; inside, `which kind k6 helm terraform trivy kubectl act kustomize shellcheck actionlint ast-grep jq hadolint` all resolve; `python --version` → `3.12.x`; `echo $DOCKER_HOST` → `unix:///run/user/1000/docker-me.sock`; `docker info` succeeds and shows `rootless`; `.env.example` documents `MODEL_PATH`, `MODEL_STORAGE_BACKEND=local`, `S3_BUCKET`, `S3_ENDPOINT_URL`, `AWS_REGION`, `API_KEY`, `OTEL_ENDPOINT`, `PROMETHEUS_URL`, `LLM_BACKEND`, `DOCKER_HOST`.
  - **QA happy:** `devenv shell -- bash -c 'docker info --format "{{.SecurityOptions}}"' > /tmp/qa/t2_docker.txt` contains `rootless`.
  - **QA failure:** `env -u DOCKER_HOST docker info > /tmp/qa/t2_docker_fail.txt 2>&1` → `permission denied` (proves the wiring is required).
  - **Commit:** `scaffold(devenv): devenv.yaml/devenv.nix with Python 3.12, full toolchain, rootless DOCKER_HOST (todo 2)`

- [x] 3. uv workspace + ruff/basedpyright/pytest/coverage config
  - **References:** root `pyproject.toml` with `[tool.uv.workspace] members=["apps/*"]`; per-app `pyproject.toml`. Runtime deps: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `scikit-learn`, `joblib`, `boto3`, `prometheus-client`, `opentelemetry-api/sdk/exporter-otlp`, `httpx`. Dev deps: `pytest`, `pytest-cov`, `pytest-asyncio`, `moto[s3,server]` (the `server` extra adds flask, required for the standalone `moto_server` mock endpoint used by todo 24's offline `terraform plan`), `ruff`, `basedpyright`. Config: `ruff.toml` (line-length 100, select E,F,I,UP,B), `[tool.basedpyright]` (typeCheckingMode="strict"), `[tool.pytest.ini_options]` (testpaths, addopts `--cov`), `[tool.coverage.run/report]`.
  - **Acceptance:** `uv sync` succeeds; `uv run python -c "import fastapi, sklearn, boto3, prometheus_client"` ok; `uv run ruff check .` clean on the skeleton; `uv run basedpyright` clean on the skeleton.
  - **QA happy:** `uv sync && uv run pytest --collect-only > /tmp/qa/t3_sync.txt` → no collection errors.
  - **QA failure:** add an unused import to a skeleton module → `uv run ruff check > /tmp/qa/t3_ruff.txt` flags `F401`.
  - **Commit:** `scaffold(workspace): uv workspace + ruff/basedpyright/pytest/coverage config (todo 3)`

- [x] 4. Self-documenting Makefile with every spec target
  - **References:** spec §5 deliverables. Targets: `help` (default, self-documenting via `## ` comments + awk), `run-local`, `test`, `lint`, `typecheck`, `docker-build`, `ci-local`, `deploy-local`, `deploy-staging`, `load-test-smoke`, `load-test-spike`, `load-test-soak`, `load-test-stress`, `deploy-canary`, `promote`, `rollback`, `eval`, `train`, `llm-cpu-smoke`, `canary-check`, `batch`. Export `DOCKER_HOST ?= unix:///run/user/1000/docker-me.sock` at the top so docker/kind targets work rootless.
  - **Acceptance:** `make help` lists every target with a description; `make -n run-local` (dry run) prints commands without error; every spec §5 `make …` command maps to a target.
  - **QA happy:** `make help > /tmp/qa/t4_help.txt` contains `run-local`, `deploy-local`, `deploy-canary`, `rollback`, `load-test-spike`.
  - **QA failure:** `make does-not-exist > /tmp/qa/t4_badtarget.txt 2>&1` → non-zero exit, `No rule to make target`.
  - **Commit:** `scaffold(make): self-documenting Makefile with all spec targets (todo 4)`

- [x] 5. Package skeletons for gateway/model-server/worker + smoke test
  - **References:** spec §3 apps layout. Create typed stub modules: `apps/model-server/src/{server,model_loader,predictor,batching,metrics,schemas}.py` + `storage/{base,local}.py`; `apps/gateway/src/{main,config,auth,rate_limit,telemetry}.py` + `routes/`; `apps/worker/src/{batch_inference,drift_job,report_generator}.py`; `__init__.py` with `__version__` in each package; top-level `tests/test_smoke.py` + `conftest.py`.
  - **Acceptance:** all three packages import (`uv run python -c "import ..."`); `uv run pytest tests/test_smoke.py` passes (asserts each package imports + has `__version__`).
  - **QA happy:** `uv run pytest tests/test_smoke.py -q > /tmp/qa/t5_smoke.txt` → `1 passed`.
  - **QA failure:** break an import in a skeleton → `uv run pytest tests/test_smoke.py > /tmp/qa/t5_smoke_fail.txt` → `ImportError`.
  - **Commit:** `scaffold(packages): gateway/model-server/worker skeletons + smoke test (todo 5)`

### Wave 2 — C2a: Fraud model + model server (Milestone 1) (exit: `make run-local` → `curl :8080/v1/predict` valid JSON; `/metrics` Prometheus format)

- [x] 6. Fraud-detection dataset + train.py + evaluate.py + config.yaml → artifact + metrics
  - **References:** spec §M1 (fraud-detection classifier). Files: `models/training/{dataset.py,train.py,evaluate.py,config.yaml}`; outputs `models/artifacts/fraud_detector/{model.pkl,metrics.json}`. **Must-NOT-Have:** no training-from-scratch research — generate a deterministic synthetic fraud-like tabular dataset (`sklearn.datasets.make_classification` seeded, or a generated imbalanced tabular set), seeded in `config.yaml`. Classifier: `sklearn.ensemble.GradientBoostingClassifier` (or `HistGradientBoosting`), persisted with `joblib.dump`.
  - **Acceptance:** `uv run python models/training/train.py` writes `model.pkl`; `uv run python models/training/evaluate.py` writes `metrics.json` with `accuracy>0.85`, `f1`, `roc_auc`; deterministic (same seed → identical metrics on re-run).
  - **QA happy:** run train+evaluate; `jq . models/artifacts/fraud_detector/metrics.json > /tmp/qa/t6_metrics.json` shows accuracy/f1/roc_auc.
  - **QA failure:** point `config.yaml` at a bad output dir → `train.py` exits non-zero with a clear typed error → `/tmp/qa/t6_train_fail.txt`.
  - **Commit:** `server(training): fraud-detection dataset/train/evaluate → artifact + metrics (todo 6)`

- [x] 7. Storage interface + LocalStorageBackend + model_loader + predictor + schemas + tests
  - **References:** **Metis fix:** define the `ModelStorage` interface + `LocalStorageBackend` HERE (so the runtime loader and the W6 registry share one interface — no W6 rework); W6 (todo 26) adds `S3StorageBackend`. Files: `apps/model-server/src/storage/{base,local}.py`, `apps/model-server/src/{model_loader,predictor,schemas}.py`. Pydantic v2. `ModelStorage` ABC: `get_artifact(model_id, version) -> Path`, `list_models() -> list[ModelInfo]`. `model_loader.load_model(storage, model_id, version)` loads via `joblib`, caches, records load time. `predictor.predict(model, features) -> {label, score}`. `schemas.py`: `PredictRequest{features}`, `PredictResponse{prediction, score, model_id, model_version}`, `BatchRequest/BatchResponse`, `ModelInfo`. Typed errors (`ModelNotFound`).
  - **Acceptance:** `model_loader` loads the todo-6 artifact via `LocalStorageBackend`; `predictor` returns `{prediction, score}`; schemas validate valid input and reject invalid; `uv run pytest apps/model-server/tests -k "loader or predictor or schema or storage_local"` passes with high coverage on these modules.
  - **QA happy:** load todo-6 model, predict on a sample row → valid `PredictResponse` → `/tmp/qa/t7_predict.txt`.
  - **QA failure:** request a nonexistent `model_id` → `model_loader` raises `ModelNotFound` → `/tmp/qa/t7_notfound.txt`.
  - **Commit:** `server(model-server): storage interface + LocalBackend, model_loader, predictor, schemas + tests (todo 7)`

- [x] 8. Dynamic batching + Prometheus metrics + tests
  - **References:** `apps/model-server/src/{batching,metrics}.py`; `prometheus-client`. Metrics: `inferenceops_request_count` (Counter, labels `method`,`status`,`model_version`), `inferenceops_request_duration_seconds` (Histogram, label `model_version`), `inferenceops_error_count` (Counter, label `model_version`), `inferenceops_model_load_seconds` (Histogram), `inferenceops_batch_size` (Histogram). **The `model_version` label is set from the loaded model at request time and is the discriminator the canary rollback (todo 30) uses to compare candidate vs stable.** `DynamicBatcher`: collect up to `max_batch_size` OR `max_wait_ms`, run predictor on the batch, fan results back; async-safe.
  - **Acceptance:** the batcher groups N concurrent requests into ≤ ⌈N/batch⌉ inference calls; the registry exposes all 5 metrics in Prometheus text format; `uv run pytest -k "batching or metrics"` passes.
  - **QA happy:** fire 10 concurrent requests with `max_batch_size=4` → assert ≤ 3 inference calls via a counter → `/tmp/qa/t8_batch.txt`.
  - **QA failure:** a request that exceeds the batcher wait window → returns 504 and `inferenceops_error_count` increments → `/tmp/qa/t8_timeout.txt`.
  - **Commit:** `server(model-server): dynamic batching + Prometheus metrics + tests (todo 8)`

- [x] 9. FastAPI server: predict/batch/health/ready/metrics/models + graceful shutdown + integration tests
  - **References:** spec §4 API: `POST /v1/predict`, `POST /v1/batch`, `GET /healthz`, `GET /readyz`, `GET /metrics`, `GET /v1/models`. `apps/model-server/src/server.py` (uses todos 7,8). **Readiness vs liveness:** `/healthz` → 200 once the process is up; `/readyz` → 503 until the model is loaded, 200 after. **Graceful shutdown:** SIGTERM handler stops accepting, drains the batcher, exits 0 (pairs with the k8s `preStop` in todo 16). Instrument every request via the todo-8 metrics decorator. Integration tests use `fastapi.testclient.TestClient`.
  - **Acceptance:** `uv run uvicorn server:app --port 8080` → `curl :8080/v1/predict` returns valid JSON; `/healthz` 200; `/readyz` 200 after load; `/metrics` Prometheus text; `/v1/models` lists `fraud-detector`; `uv run pytest apps/model-server/tests/test_server.py` passes; SIGTERM → clean exit 0.
  - **QA happy:** start server; `curl` `/v1/predict` + `/readyz` + `/metrics` → 200 + valid → `/tmp/qa/t9_endpoints.txt`.
  - **QA failure:** `curl /v1/predict` with an invalid schema → 422; start with a missing model file → `/readyz` 503 until loaded → `/tmp/qa/t9_fail.txt`.
  - **Commit:** `server(model-server): FastAPI predict/batch/health/ready/metrics/models + graceful shutdown + tests (todo 9)`

- [x] 10. model-server Dockerfile (multi-stage, non-root) + container build test
  - **References:** **Metis:** build with `DOCKER_HOST=unix:///run/user/1000/docker-me.sock`. `apps/model-server/Dockerfile` + `.dockerignore`. Base `python:3.12-slim`; multi-stage (builder installs deps via `uv sync --frozen`; runtime copies the venv + src + artifact); create + `USER` a non-root user; `EXPOSE 8080`; `HEALTHCHECK CMD curl -f localhost:8080/healthz`; `CMD uvicorn server:app --host 0.0.0.0 --port 8080`.
  - **Acceptance:** `make docker-build` succeeds; `docker run` → container `/healthz` returns 200; `docker run --rm <image> whoami` is NOT root.
  - **QA happy:** build + run + `curl :8080/healthz` → 200 → `/tmp/qa/t10_build.txt`.
  - **QA failure:** introduce a syntax error in `server.py` → build/container fails the healthcheck → `/tmp/qa/t10_build_fail.txt`.
  - **Commit:** `server(model-server): multi-stage non-root Dockerfile + build test (todo 10)`

### Wave 3 — C2b: Gateway + compose + CI + evals (Milestone 2) (exit: `make test` + `make ci-local` pass; eval gate blocks an injected regression)

- [x] 11. API gateway: routing + API-key auth + token-bucket rate-limit + OTel telemetry + tests
  - **References:** `apps/gateway/src/{main,config,auth,rate_limit,telemetry}.py` + `routes/`. `config.py`: Pydantic Settings (`MODEL_SERVER_URL`, `API_KEYS`, `RATE_LIMIT_RPS`, `OTEL_ENDPOINT`, `LLM_BACKEND`). `auth.py`: `X-API-Key` middleware → 401 on invalid. `rate_limit.py`: token-bucket per key/IP → 429 + `Retry-After` on exceed. `telemetry.py`: OTel tracer/meter init, span per request. `routes/`: proxy `/v1/predict`, `/v1/batch`, `/v1/models` to model-server, and **reserve** `/v1/completions`, `/v1/chat/completions` for the LLM route added in todo 43 (**Metis:** design the route table so the LLM route bolts on cleanly). `main.py` middleware order: telemetry → auth → rate-limit → route.
  - **Acceptance:** gateway proxies `/v1/predict` to a mocked model-server; invalid key → 401; exceeding the rate → 429 + `Retry-After`; OTel spans created; `uv run pytest apps/gateway/tests` passes.
  - **QA happy:** with a mocked upstream + valid key → 200 proxied response → `/tmp/qa/t11_proxy.txt`.
  - **QA failure:** invalid key → 401; burst over the limit → 429 → `/tmp/qa/t11_auth_rl.txt`.
  - **Commit:** `gateway: routing + API-key auth + token-bucket rate-limit + OTel telemetry + tests (todo 11)`

- [x] 12. gateway Dockerfile + docker-compose stack (model-server + gateway + prometheus + grafana)
  - **References:** **Metis:** `DOCKER_HOST` for compose; Grafana is provisioned here with a datasource + a dashboard *provider* (file mounts); the four dashboard JSONs are created in todo 33 (single JSON source reused by the Helm path). `docker-compose.yml`; `observability/prometheus/prometheus.yml` (scrape `model-server:8080`, `gateway:8080`); `observability/grafana/provisioning/{datasources/datasource.yml, dashboards/dashboards.yml}` (datasource uid `prometheus`; dashboard provider → `/var/lib/grafana/dashboards`).
  - **Acceptance:** `docker compose up -d --build` brings all 4 services healthy; `gateway:8080/v1/predict` (with key) → 200; `prometheus:9090/api/v1/targets` shows model-server `UP`; `grafana:3000/api/datasources` returns `prometheus` and the dashboard provider is configured (`grafana:3000/api/search` returns 200 — an empty result is OK here; the four real dashboards are asserted in todo 33).
  - **QA happy:** compose up; gateway predict 200; prometheus target UP; grafana `/api/search` non-empty → `/tmp/qa/t12_compose.txt`.
  - **QA failure:** stop model-server → gateway predict 502 + prometheus target `DOWN` → `/tmp/qa/t12_compose_fail.txt`.
  - **Commit:** `gateway(docker): Dockerfile + docker-compose stack with prometheus + grafana provisioning (todo 12)`

- [x] 13. Evals: golden dataset + run_eval + latency_gate + regression_gate
  - **References:** spec §M2/§5 eval gate. `evals/{golden_dataset.jsonl,run_eval.py,latency_gate.py,regression_gate.py}`; `scripts/run_evals.sh`; baseline `evals/reports/baseline.json`. Golden dataset = 100+ fraud cases `{features, expected}`.
  - **Acceptance:** `make eval` runs all three gates; PASSES on the trained model (accuracy>0.85, p95<100ms, no regression vs baseline); a deliberately degraded model FAILS `regression_gate.py` (accuracy/f1 drop > 3%).
  - **QA happy:** `run_eval.py` on the good model → PASS, report written → `evals/reports/eval_*.json` + `/tmp/qa/t13_eval.txt`.
  - **QA failure:** point at a bad model / tighten baseline → `regression_gate.py` exits non-zero `REGRESSION DETECTED` → `/tmp/qa/t13_gate_fail.txt`.
  - **Commit:** `ci(evals): golden dataset + run_eval + latency_gate + regression_gate (todo 13)`

- [x] 14. GitHub Actions workflows: ci + build-and-push + model-eval-gate (actionlint + act)
  - **References:** **Metis act limitations:** `actionlint` validates ALL workflows; the **executable proof** is running the CI steps **directly** (`make lint typecheck test` via uv + `make docker-build` via docker, with `DOCKER_HOST`); `act` is **best-effort** for the non-build jobs and is **actionlint-only for the docker-build job** (docker-in-docker under rootless docker on a custom socket is a known failure mode); `build-and-push.yml` push step + `model-eval-gate.yml` (PR triggers/secrets) are **actionlint-only** (cannot push to a registry or fully emulate PR contexts under `act`). `.github/workflows/{ci.yml,build-and-push.yml,model-eval-gate.yml}`. Trivy scan: `trivy image --severity HIGH,CRITICAL --exit-code 1`. ECR push gated on `main` + secret presence. No inline secrets.
  - **Acceptance:** `actionlint .github/workflows/*.yml` clean; the CI steps run green **directly** (`make lint typecheck test` + `make docker-build`); `act` runs the non-build jobs where it works under rootless docker (best-effort, not a gate); workflows reference secrets only via `${{ secrets.* }}`/OIDC, never inline.
  - **QA happy:** `actionlint > /tmp/qa/t14_actionlint.txt` clean; direct `make lint typecheck test docker-build` pass → `/tmp/qa/t14_direct.txt` (plus `act` non-build jobs where they run → `/tmp/qa/t14_act.txt`).
  - **QA failure:** inject `run: exit 1` into a ci step → the direct `make` run of that step fails (and `act`, where used, fails) → `/tmp/qa/t14_act_fail.txt`.
  - **Commit:** `ci(workflows): ci + build-and-push (Trivy, gated ECR) + model-eval-gate; actionlint + act clean (todo 14)`

- [x] 15. Coverage gate enforcement (model-server ≥80% / gateway ≥70%)
  - **References:** prior-plan M4. `pyproject.toml` `[tool.coverage]`; CI `coverage` job runs per-package `uv run pytest --cov … --cov-fail-under …`. Ensure todos 7–11 tests actually meet the thresholds (add tests if short).
  - **Acceptance:** `make test` reports coverage; model-server ≥ 80%, gateway ≥ 70%; the CI coverage job is green; removing a test drops coverage below threshold and the job fails.
  - **QA happy:** coverage report shows ≥80% / ≥70% → `/tmp/qa/t15_cov.txt`.
  - **QA failure:** temporarily delete a test file → coverage < threshold → CI step exits non-zero → `/tmp/qa/t15_cov_fail.txt`.
  - **Commit:** `ci(coverage): enforce model-server>=80% / gateway>=70% coverage gates (todo 15)`

### Wave 4 — C3: Kubernetes + autoscaling (Milestones 3, 6) (exit: `make deploy-local` → pods Running on kind; `make load-test-spike` → HPA 2→5+ then scale-down)

- [x] 16. k8s/base core manifests (namespace, sa, configmap, secret.example, per-app deployments+services, kustomization)
  - **References:** spec §M3. `k8s/base/{namespace,serviceaccount,configmap,secret.example,kustomization}.yaml` + per-app manifests `deployment-model-server.yaml`, `deployment-gateway.yaml`, `service-model-server.yaml`, `service-gateway.yaml` (**Oracle F2:** the stack deploys BOTH apps — the gateway is the auth/rate-limit entrypoint that proxies to model-server in-cluster — so each needs its own Deployment + Service; the canary (todo 29) adds a third `model-server-canary` Deployment). **Metis:** `imagePullPolicy: IfNotPresent` (kind has no registry; images are `kind load`'d in todo 19). The model-server Deployment MUST show (gateway is similar, lighter on resources): `resources` requests `cpu:2,memory:4Gi` / limits `cpu:4,memory:8Gi`; `readinessProbe`→`/readyz`, `livenessProbe`→`/healthz`; `strategy.rollingUpdate {maxUnavailable:0, maxSurge:1}`; `lifecycle.preStop exec sleep 10` + `terminationGracePeriodSeconds: 30` (graceful shutdown, pairs with todo 9); `affinity.podAntiAffinity` (preferred). GPU nodeSelector/tolerations live in the **prod overlay** (todo 18), not base.
  - **Acceptance:** `kubectl kustomize k8s/base` renders valid YAML; `kubectl apply --dry-run=client` succeeds; deployment has probes + resources + rollingUpdate + preStop + anti-affinity + `imagePullPolicy: IfNotPresent`.
  - **QA happy:** `kubectl kustomize k8s/base | kubectl apply --dry-run=client -f - > /tmp/qa/t16_kustomize.txt` → no errors.
  - **QA failure:** set an invalid resource quantity (`cpu: "abc"`) → dry-run fails → `/tmp/qa/t16_kustomize_fail.txt`.
  - **Commit:** `k8s(base): namespace/sa/configmap/secret/deployment(probes,resources,rolling,preStop,anti-affinity)/service (todo 16)`

- [x] 17. k8s/base ingress + HPA (CPU+memory) + PDB
  - **References:** `k8s/base/{ingress,hpa,pdb}.yaml`. Ingress `ingressClassName: nginx`, host `inferenceops.local`, path `/` → **`service-gateway`:8080** (the base request path is `ingress → gateway → model-server`; **Oracle r3:** name the backend explicitly so it is unambiguous that the base ingress fronts the gateway, not the model-server). HPA v2: `minReplicas:2, maxReplicas:10`, metrics cpu 70% + memory 80% (the **custom-RPS** metric is added in todo 20 after prometheus-adapter — **Metis:** keep base HPA cpu/memory here). PDB `minAvailable:1`.
  - **Acceptance:** `kubectl kustomize k8s/base` includes Ingress + HPA + PDB; dry-run OK; HPA has cpu + memory metrics; PDB `minAvailable:1`.
  - **QA happy:** dry-run renders HPA with 2 metrics + PDB → `/tmp/qa/t17_hpa.txt`.
  - **QA failure:** set `maxReplicas < minReplicas` → validation fails → `/tmp/qa/t17_hpa_fail.txt`.
  - **Commit:** `k8s(base): nginx ingress + HPA(cpu/mem) + PDB(minAvailable=1) (todo 17)`

- [x] 18. k8s overlays local/staging/prod
  - **References:** `k8s/overlays/{local,staging,prod}/{kustomization.yaml,patches}`. **local** (kind): `replicas:2`, `imagePullPolicy: Never`, image tag `dev`. **staging**: `replicas:2`, CI image tag. **prod**: `replicas:3`, bumped resources, `nodeSelector {nvidia.com/gpu.present: "true"}` + toleration `nvidia.com/gpu=present:NoSchedule` for the GPU pool, **required** pod anti-affinity, `imagePullPolicy: IfNotPresent`.
  - **Acceptance:** `kubectl kustomize k8s/overlays/{local,staging,prod}` each render valid YAML; prod has the GPU toleration + nodeSelector; local has `imagePullPolicy: Never`.
  - **QA happy:** all three overlays kustomize + dry-run clean → `/tmp/qa/t18_overlays.txt`.
  - **QA failure:** a patch references a nonexistent base resource → `kustomize build` fails → `/tmp/qa/t18_overlays_fail.txt`.
  - **Commit:** `k8s(overlays): local(kind)/staging/prod(GPU toleration, required anti-affinity) overlays (todo 18)`

- [x] 19. kind bootstrap: deploy_local.sh (ingress + kube-prometheus-stack + kind load) + make deploy-local
  - **References:** **Metis:** Prometheus moves HERE; `kind load docker-image`; ingress via `extraPortMappings`. **Oracle S4:** under rootless docker, binding host ports <1024 needs `sysctl net.ipv4.ip_unprivileged_port_start=0`; to avoid that dependency, **map high host ports by default** (`8080:80`, `8443:443`) and curl the ingress via `localhost:8080`/`localhost:8443` (document the `ip_unprivileged_port_start=0` alternative for using 80/443 directly). `infra/scripts/kind-config.yaml` (control-plane + `extraPortMappings` `8080:80`/`8443:443` + `node-labels: ingress-ready=true`). **Oracle B2:** this todo CREATES a minimal `observability/prometheus/values.yaml` (grafana sidecar enabled + default datasource + ServiceMonitor selection); todo 32 EXTENDS it with alert rules via `helm upgrade`. `scripts/deploy_local.sh`: (0) write the minimal `observability/prometheus/values.yaml` if absent; (1) `kind create cluster --config` (with `DOCKER_HOST`); (2) `kind load docker-image inferenceops/model-server:dev inferenceops/gateway:dev`; (3) `kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/kind/deploy.yaml` + wait ready; (4) `helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm install kube-prometheus prometheus-community/kube-prometheus-stack --version 87.19.0 -n monitoring --create-namespace -f observability/prometheus/values.yaml` + wait ready; (5) `kubectl apply -k k8s/overlays/local` + wait pods Ready. **First-run kind create is the gate** that verifies kind works under this rootless docker.
  - **Acceptance:** `make deploy-local` → kind cluster up; ingress-nginx controller Running; kube-prometheus-stack Prometheus + Grafana Running; model-server + gateway pods `2/2 Running`; `kubectl get pods -n inferenceops` all Running; probes pass.
  - **QA happy:** `kubectl get pods -A > /tmp/qa/t19_pods.txt` shows ingress-nginx, monitoring, inferenceops pods Running.
  - **QA failure:** skip `kind load` → pod `ErrImageNeverPull` (the local overlay uses `imagePullPolicy: Never`; with `IfNotPresent` it would be `ImagePullBackOff`) — demonstrates why `kind load` is required → `/tmp/qa/t19_pull_fail.txt`.
  - **Commit:** `k8s(kind): deploy_local.sh — kind + ingress + kube-prometheus-stack + kind-load + overlay (todo 19)`

- [x] 20. Autoscaling: prometheus-adapter custom-RPS HPA + KEDA ScaledObject + lightweight spike load-gen; Karpenter EKS-ref only
  - **References:** **Metis:** Karpenter is EKS-only (no cloud provider on kind) → doc/ref only; the spike load-gen must NOT depend on k6 (W8) → use a `kubectl run` curl/`hey` loop. **Oracle S2:** prometheus-adapter is installed via Helm with an explicit `values.yaml` whose `rules.external` maps the `inferenceops_request_count` rate to an external metric `inferenceops_rps` (e.g. `rules.external: [{seriesQuery: 'inferenceops_request_count{namespace!="",pod!=""}', resources: {overrides: {namespace:{resource:"namespace"}, pod:{resource:"pod"}}}, name: {as: 'inferenceops_rps'}, metricsQuery: 'sum(rate(<<.Series>>{<<.LabelMatchers>>}[1m])) by (<<.GroupBy>>)'}]`); the `hpa-custom` overlay **merges** the external metric INTO the base cpu/memory HPA (it does NOT replace them), so an `<unknown>` external metric still leaves cpu/memory driving scaling. KEDA 2.20 ScaledObject **prometheus trigger uses `query` + `threshold` (NOT `metricName`)**: `serverAddress: http://kube-prometheus-kube-prome-prometheus.monitoring.svc:9090`, `query: sum(rate(inferenceops_request_count[1m]))`, `threshold`. KEDA install: `helm repo add kedacore https://kedacore.github.io/charts && helm install keda kedacore/keda --version 2.20.1 -n keda --create-namespace`. `scripts/spike_load.sh` = lightweight 60s load loop. Karpenter documented in `docs/autoscaling-strategy.md` + realized as Terraform (todo 23).
  - **Acceptance:** `make load-test-spike` (runs `spike_load.sh`) → `kubectl get hpa -w` shows model-server scaling 2→5+; the KEDA ScaledObject is `ACTIVE`; replicas scale down after traffic stops; Karpenter is documented as EKS-only.
  - **QA happy:** spike → HPA 2→5+, then scale-down → `/tmp/qa/t20_hpa.txt` + `/tmp/qa/t20_keda.txt`.
  - **QA failure:** with Prometheus unreachable, the custom-metric HPA reports `<unknown>` and falls back to cpu/memory (documented) → `/tmp/qa/t20_hpa_degraded.txt`.
  - **Commit:** `k8s(autoscaling): prometheus-adapter custom-RPS HPA + KEDA ScaledObject + spike load-gen; Karpenter EKS-ref (todo 20)`

- [x] 21. Kubernetes ML-workload hardening verification + docs
  - **References:** spec §M3 "important Kubernetes details". `scripts/verify_k8s.sh`; `docs/kubernetes-ml-workloads.md`. Assert via `kubectl get -o jsonpath` that the deployed deployment has probes, resources, rollingUpdate, preStop; PDB + HPA exist.
  - **Acceptance:** `verify_k8s.sh` passes all assertions on the kind deployment; `docs/kubernetes-ml-workloads.md` covers pod lifecycle, readiness vs liveness, requests vs limits, HPA vs cluster-autoscaler vs Karpenter, services vs ingress, taints/tolerations, node selectors, GPU scheduling (`nvidia.com/gpu` limit + GPU node-group taint/toleration) — referencing the actual manifests + the prod overlay + EKS Terraform.
  - **QA happy:** `scripts/verify_k8s.sh > /tmp/qa/t21_verify.txt` → all PASS.
  - **QA failure:** remove the `preStop` hook → `verify_k8s.sh` fails that assertion → `/tmp/qa/t21_verify_fail.txt`.
  - **Commit:** `k8s(docs): verify_k8s.sh hardening assertions + kubernetes-ml-workloads.md (todo 21)`

### Wave 5 — C4: AWS infrastructure IaC (Milestone 4) (exit: `terraform validate` for all envs + offline `terraform plan` against the local moto mock endpoint pass for dev/staging/prod; scripts shellcheck-clean)

- [x] 22. Terraform modules: vpc + ecr
  - **References:** VPC module `terraform-aws-modules/vpc/aws ~> 6.6` — `name`, `cidr`, `azs`, `public_subnets`, `private_subnets`, `public_subnet_tags = {"kubernetes.io/role/elb"=1}`, `private_subnet_tags = {"kubernetes.io/role/internal-elb"=1}`, `enable_nat_gateway=true`, `one_nat_gateway_per_az=true`. ECR: 3 `aws_ecr_repository` (gateway, model-server, worker) with `image_scanning_configuration{scan_on_push=true}` + `image_tag_mutability="IMMUTABLE"`. `infra/terraform/modules/{vpc,ecr}/` each with `main.tf,variables.tf,outputs.tf,README.md` (variables + outputs described).
  - **Acceptance:** `terraform init && terraform validate` pass in each module (module level — `terraform plan` is the env-level gate in todo 24, since a standalone module inherits its provider from the calling root and has no provider config of its own).
  - **QA happy:** `terraform validate > /tmp/qa/t22_validate.txt` clean.
  - **QA failure:** reference an undefined variable → `terraform validate` fails → `/tmp/qa/t22_validate_fail.txt`.
  - **Commit:** `infra(terraform): vpc + ecr modules (validate-clean) (todo 22)`

- [x] 23. Terraform modules: eks (+optional GPU node group, Karpenter submodule) + iam (IRSA via iam-role v6)
  - **References:** EKS module `terraform-aws-modules/eks/aws ~> 21.24` — **v21 renames:** `name` (was `cluster_name`), `kubernetes_version` (was `cluster_version`; pin `"1.33"`, default `null`), `addons` (was `cluster_addons`); `enable_irsa=true` (default) creates the OIDC provider; `eks_managed_node_groups` is a map with `taints = map(object({key,value,effect}))`. CPU group `m6i.xlarge` 2–6; GPU group `g5.xlarge`, `ami_type="BOTTLEROCKET_x86_64_NVIDIA"`, min/desired 0, taint `nvidia.com/gpu=present:NO_SCHEDULE`, gated by `var.enable_gpu_node_group` (default `false`). **Oracle r3 offline guards (wired by the env in todo 24):** each managed node group exposes `use_latest_ami_release_version` so the env sets it `false` offline (avoids the SSM AMI data source moto cannot serve), and `addons` are omitted offline (apply-time only). **Karpenter:** standalone repo is gone → use `terraform-aws-modules/eks/aws//modules/karpenter` with `create_pod_identity_association=true` (v21 dropped IRSA vars), gated by `var.enable_karpenter` (default `false`). IAM module `terraform-aws-modules/iam/aws//modules/iam-role ~> 6.6` — **`iam-assumable-role-with-oidc` was removed in v6 → use `iam-role`** with `enable_oidc=true`, `oidc_provider_urls=[module.eks.oidc_provider]`, `oidc_subjects=["system:serviceaccount:inferenceops:model-server"]`, `policies = {S3ReadOnly="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"}`. **Oracle S11:** before executing, verify the v6 `iam-role` module's actual OIDC variable names against the registry/docs (the librarian cited `UPGRADE-6.0.md` — `iam-assumable-role-with-oidc` merged into `iam-role` — plus `variables.tf` permalinks for `enable_oidc`/`oidc_provider_urls`/`oidc_subjects`/`policies`); if `iam-assumable-role-with-oidc` still ships in the pinned v6, use it instead (`provider_url`, `oidc_fully_qualified_subject_arns`, `role_policy_arns`). `enable_irsa` is gated off in offline mode (todo 24) because the OIDC `tls_certificate` data source needs a reachable HTTPS endpoint. Outputs: `oidc_provider_arn`, `cluster_endpoint`.
  - **Acceptance:** `terraform validate` clean (module level — `terraform plan` is the env-level gate in todo 24); the module supports `enable_gpu_node_group=false` (default) AND `=true`; the IAM role trusts the cluster OIDC provider for the `model-server` service account (verified at env level in todo 24).
  - **QA happy:** `terraform validate` clean with `enable_gpu_node_group=false` and `=true` (module level; env-level `plan` is todo 24) → `/tmp/qa/t23_eks.txt`.
  - **QA failure:** set `kubernetes_version="9.99"` → `terraform validate`/type check errors → `/tmp/qa/t23_eks_fail.txt`.
  - **Commit:** `infra(terraform): eks (cpu + optional gpu node group, Karpenter submodule) + iam IRSA (iam-role v6) (todo 23)`

- [x] 24. Terraform modules: s3 + alb + monitoring + OFFLINE mock provider + envs dev/staging/prod
  - **References:** **Oracle B1:** `skip_credentials_validation`/`skip_requesting_account_id`/`skip_metadata_api_check` + mock keys suppress only the provider's OWN init calls — they do NOT stop **data sources**, which are read during `plan`. The EKS module v21 reads `data "aws_ssm_parameter"` (EKS-optimized AMI → SSM `GetParameter`) and `data "tls_certificate"` (IRSA OIDC → live HTTPS fetch of the cluster's OIDC issuer cert), and likely `data "aws_caller_identity"` (STS) — all fail with no creds/network. So a pure mock provider cannot `plan` EKS offline. **Fix adopted (real local mock):** stand up `moto_server` (provided by `moto[s3,server]` — the `server` extra adds flask, required for the standalone server; there is no `moto server` subcommand) on `http://localhost:5000`, invoked as `moto_server -p 5000` (or `uv run python -m moto.server -p 5000`); add a provider `endpoints` block (guarded by `var.offline_plan`, default `true`) pointing `sts, iam, ec2, eks, s3, elbv2, cloudwatch, logs, ssm` at it, PLUS `skip_credentials_validation=true; skip_requesting_account_id=true; skip_metadata_api_check=true; access_key="mock"; secret_key="mock"`. `scripts/tf_mock.sh` starts `moto_server -p 5000` (moto then answers the data sources that DO have mockable APIs — `aws_caller_identity` via STS, `aws_availability_zones` via EC2; `aws_partition`/`aws_region` resolve locally). **Oracle r3 (empirically proven on moto 5.2.2):** do NOT seed the EKS-optimized AMI SSM parameter — moto **rejects** `PutParameter` under the reserved `/aws/…` namespace (`AccessDeniedException`), and `data "aws_eks_addon_version"` is read for **every** configured addon regardless of a pinned `addon_version` while moto returns 404 for `DescribeAddonVersions`. So the offline plan must AVOID these data sources rather than mock them (there is deliberately no `tf_mock_seed.py` — the `/aws/` seed is impossible). **EKS offline guards (these AVOID the un-mockable data sources so offline `plan` genuinely succeeds):** (1) `use_latest_ami_release_version = false` on every managed node group offline — the module's AMI data source is gated `count = var.create && var.use_latest_ami_release_version ? 1 : 0`, and `use_latest_ami_release_version` defaults `true`, so it MUST be set `false` offline to stop the SSM AMI read; (2) `addons = {}` (omitted) offline — addons are apply-time only, because `data "aws_eks_addon_version"` is read for all configured addons regardless of pinning and moto 404s on `DescribeAddonVersions`; (3) `enable_irsa = !var.offline_plan` — the `tls_certificate` data source needs a reachable HTTPS OIDC issuer (document that the OIDC provider + IRSA are created on real `apply`). With these three guards the only data sources read offline are `aws_caller_identity` (STS→moto), `aws_availability_zones` (EC2→moto) and `aws_partition`/`aws_region` (local) — all satisfied — so `terraform plan` succeeds offline. **Contingency (not the primary path):** if some unforeseen data source still cannot be mocked, that specific env degrades to `terraform validate`-only (documented); the asserted gate is offline `plan` success via the three guards. S3 module `terraform-aws-modules/s3-bucket/aws ~> 5.15` — `versioning={enabled=true}`, `server_side_encryption_configuration` (AES256). ALB module (`aws_lb` internet-facing + target group + listener, or document the AWS Load Balancer Controller annotation approach). Monitoring module (`aws_cloudwatch_log_group` + `aws_cloudwatch_metric_alarm` for 5xx/latency). `envs/{dev,staging,prod}/{main.tf,variables.tf,providers.tf,terraform.tfvars.example}` wire all modules (dev: gpu=false, karpenter=false, offline_plan=true; prod: toggles exposed). Constraints: `terraform >= 1.5.7`, `aws >= 6.52`.
  - **Acceptance:** `terraform validate` passes all 3 envs offline (no creds); with `scripts/tf_mock.sh` running (moto_server) and the three EKS offline guards active (`use_latest_ami_release_version=false` on node groups, `addons={}` omitted, `enable_irsa=false`), `terraform plan` succeeds **offline** for dev/staging/prod against the moto endpoint; setting `offline_plan=false` with no creds fails auth (documented as the real-AWS path that creates the OIDC provider/IRSA).
  - **QA happy:** start `scripts/tf_mock.sh` (moto_server); with no AWS creds in env and the three offline guards active, `terraform plan` for dev succeeds against the moto endpoint → `/tmp/qa/t24_plan.txt`.
  - **QA failure:** stop moto (no endpoint) while `offline_plan=true` → `terraform plan` fails on a data-source read (proves the mock endpoint is required); separately, `offline_plan=false` with no creds → auth error (proves the real-AWS guard) → `/tmp/qa/t24_plan_fail.txt`.
  - **Commit:** `infra(terraform): s3 + alb + monitoring modules + moto-backed offline plan (endpoints + 3 EKS guards: use_latest_ami_release_version=false, addons omitted, enable_irsa off) + dev/staging/prod envs (todo 24)`

- [x] 25. Infra scripts (bootstrap, gpu-plugin, prometheus, destroy) + deploy_staging.sh (shellcheck-clean, dry-run)
  - **References:** `infra/scripts/{bootstrap_cluster.sh,install_gpu_plugin.sh,install_prometheus.sh,destroy_dev.sh}`; `scripts/deploy_staging.sh`. **Metis:** `deploy_staging.sh` is NOT executed (apply is manual) → shellcheck + `--dry-run` only. bootstrap = metrics-server + ingress-nginx + cert-manager; gpu-plugin = NVIDIA device-plugin DaemonSet; prometheus = kube-prometheus-stack Helm; destroy = `terraform destroy` dev with confirmation.
  - **Acceptance:** `shellcheck infra/scripts/*.sh scripts/deploy_staging.sh` clean; `deploy_staging.sh --dry-run` prints the actions it WOULD take without applying.
  - **QA happy:** shellcheck clean; `deploy_staging.sh --dry-run` prints the plan → `/tmp/qa/t25_shellcheck.txt`.
  - **QA failure:** inject an unquoted variable → shellcheck flags it → `/tmp/qa/t25_shellcheck_fail.txt`.
  - **Commit:** `infra(scripts): bootstrap/gpu-plugin/prometheus/destroy + deploy_staging (dry-run); shellcheck-clean (todo 25)`

### Wave 6 — C5: MLOps lifecycle + canary/rollback (Milestones 5, 8) (exit: registry round-trip works; canary routes ~10%; synthetic regression triggers auto-rollback; metrics outage triggers conservative rollback)

- [x] 26. S3StorageBackend + storage abstraction tests (moto)
  - **References:** **Metis:** the `ModelStorage` interface + `LocalStorageBackend` already exist (todo 7); here add `S3StorageBackend` with **`endpoint_url` injection** (env `S3_ENDPOINT_URL`) so moto/localstack and real S3 share one code path, and credential resolution supports both IRSA (default boto chain) and static keys. `apps/model-server/src/storage/s3.py`; `apps/model-server/tests/test_storage_backends.py` (Local real tempdir; S3 via `moto[s3]` `mock_aws`).
  - **Acceptance:** `S3StorageBackend` satisfies the same `ModelStorage` contract as Local; `test_storage_backends.py` passes (Local real + S3 mocked via moto); `endpoint_url` injection works for both backends.
  - **QA happy:** moto round-trip put/get artifact → bytes match → `/tmp/qa/t26_s3.txt`.
  - **QA failure:** wrong bucket → `S3StorageBackend` raises a typed `StorageError` → `/tmp/qa/t26_s3_fail.txt`.
  - **Commit:** `registry(storage): S3StorageBackend (endpoint_url injection) + moto-backed storage tests (todo 26)`

- [x] 27. Model registry: register_model + promote_model + model_card template
  - **References:** spec §M5 metadata schema `{model_id, version, artifact_uri, framework, metrics{accuracy,f1,roc_auc}, latency_p95_ms, approved_for_prod}`. `models/registry/{register_model.py,promote_model.py,model_card_template.md}`. Registry metadata is JSON (local `registry.json` dev / S3 prod) via the storage abstraction; **model versioning is decoupled from git semver** (prior-plan M9).
  - **Acceptance:** `register_model.py` writes a metadata JSON with the exact schema (`approved_for_prod:false`); `promote_model.py --to staging` sets status; `--to prod` requires the eval gate to have passed (`approved_for_prod`); a model card is filled from the template.
  - **QA happy:** register v1 → valid metadata JSON; `promote --to staging` → `status=staging` → `/tmp/qa/t27_registry.txt`.
  - **QA failure:** `promote --to prod` without a passing eval gate → rejected → `/tmp/qa/t27_promote_fail.txt`.
  - **Commit:** `registry: register_model + promote_model + model_card template (decoupled versioning) (todo 27)`

- [x] 28. S3 upload workflow + model version metadata + run_evals.sh wiring
  - **References:** **Metis:** an S3 upload that needs real creds is **actionlint-only** if it's CI, OR a moto-backed local script. `scripts/upload_model.sh` (uploads the artifact via the storage abstraction; works against moto locally, real S3 with creds); `.github/workflows/model-upload.yml` (on tag: upload to S3 — actionlint-validated, NOT act-executed); wire `scripts/run_evals.sh` (todo 13) into the promotion flow.
  - **Acceptance:** `upload_model.sh` against moto uploads + registers an `artifact_uri` (`s3://…`); `model-upload.yml` is actionlint-clean; `run_evals.sh` runs the full gate suite.
  - **QA happy:** `upload_model.sh` (moto) → `artifact_uri` registered → `/tmp/qa/t28_upload.txt`.
  - **QA failure:** upload with no backend configured → clear typed error → `/tmp/qa/t28_upload_fail.txt`.
  - **Commit:** `registry(upload): upload_model.sh (moto/real S3) + model-upload workflow (actionlint) + run_evals wiring (todo 28)`

- [x] 29. Canary: nginx canary-weight manifests + make deploy-canary/promote/rollback
  - **References:** nginx canary annotations (controller v1.15.1): `nginx.ingress.kubernetes.io/canary: "true"` + `nginx.ingress.kubernetes.io/canary-weight: "10"`; **both Ingresses must share the same `host`**; `ingressClassName: nginx`. `k8s/canary/{canary-deployment.yaml,canary-ingress.yaml,stable-ingress.yaml}`; `scripts/deploy_canary.sh`; `serving/fastapi/deployment.md`. **Oracle F2 (request topology & canary tier — one coherent flow):** the BASE path is `ingress → service-gateway → service-model-server` (todo 17's base ingress fronts `service-gateway`). For the canary demo, the canary `stable-ingress` **replaces** the base ingress for host `inferenceops.local` (they do NOT coexist — coexisting same-host ingresses with different backends is undefined nginx behavior) and fronts the model-server tier directly: `stable-ingress` → `service-model-server` (stable) and `canary-ingress` → `service-model-server-canary` (candidate, `canary-weight: "10"`, same host); during the canary demo the gateway (auth/rate-limit) is **bypassed** so the split happens cleanly at the model-server tier, and auth/rate-limit are restored when the base ingress is reinstated on promote/rollback. Candidate-vs-stable is discriminated by the `model_version` metric label (todo 8) regardless of routing path. Makefile targets `deploy-canary MODEL_VERSION=`, `promote`, `rollback` (todo 4).
  - **Acceptance:** `make deploy-canary MODEL_VERSION=v4` → canary Ingress at weight 10; ~10% of requests carry the candidate `model_version` (verify via a response header/metric); `make promote` → 100% candidate; `make rollback` → 0% candidate (stable only).
  - **QA happy:** deploy-canary; send 100 requests; ~10% carry the candidate `model_version` → `/tmp/qa/t29_canary.txt`.
  - **QA failure:** `make rollback` → 0% candidate → `/tmp/qa/t29_rollback.txt`.
  - **Commit:** `canary: nginx canary-weight manifests + deploy-canary/promote/rollback targets (todo 29)`

- [x] 30. Automated rollback: canary_check.sh (metric thresholds) + Prometheus-down conservative fallback
  - **References:** **Metis:** canary metric checks need in-cluster Prometheus (now available from todo 19); Prometheus-down fallback after 60s. spec §M8 rollback triggers: p95 +20%, error rate >1%, eval score −3%, GPU-mem threshold. **Oracle S1:** candidate-vs-stable is discriminated by the `model_version` metric label (added in todo 8) — `canary_check.sh` filters `inferenceops_*{model_version="<candidate>"}` vs `{model_version="<stable>"}`. `scripts/canary_check.sh` queries in-cluster Prometheus for candidate-vs-stable p95 (`histogram_quantile` on `inferenceops_request_duration_seconds_bucket` filtered by `model_version`), error rate (`rate(inferenceops_error_count[1m])` by `model_version`), eval score (`run_evals`), GPU mem (DCGM if present); on breach → `make rollback`. **Oracle S8 (fail-closed):** ANY unexpected error/ambiguity in `canary_check.sh` (script bug, malformed/empty query result, `make rollback` failure) → conservative rollback to last-known-good — the same path as Prometheus-unreachable >60s; the script never fails open. **Oracle S9 (automation driver):** the check runs on a **30s interval** — `scripts/deploy_canary.sh` starts a `while true; do scripts/canary_check.sh; sleep 30; done` loop for the demo window (documented alternative: a k8s `CronJob` running `canary_check.sh`). **Oracle F3 (loop termination):** the loop is **terminal** — `canary_check.sh` writes a `promote-ready` marker after N consecutive healthy checks and `deploy_canary.sh` exits the loop once `promote-ready` is reported; `make promote` first stops the check loop (kills it / clears the marker) before shifting weight to 100%, so the degenerate post-promote case (stable traffic → 0 → empty candidate-vs-stable query → fail-closed) cannot roll back a just-promoted version. **Oracle r3 (hardening):** `make promote` waits for / locks out any in-flight `canary_check.sh` iteration before shifting weight, so a concurrent check cannot race the promotion. **Oracle S7:** rollback is automated; **promotion is a deliberate gated step** — after N consecutive healthy checks over the observation window, `canary_check.sh` reports `CANARY HEALTHY → promote-ready` and the operator runs `make promote` (no silent auto-promote). Makefile target `canary-check`.
  - **Acceptance:** healthy candidate → `canary_check` passes and reports promote-ready after the clean observation window (no rollback, no auto-promote); a synthetic p95 spike >20% (filtered by `model_version`) → triggers rollback; stopping Prometheus >60s → conservative rollback fires; ANY unexpected check error → conservative rollback (fail-closed); the check runs on a 30s interval and **terminates on promote-ready** (and `make promote` stops it first), so no post-promote spurious rollback occurs.
  - **QA happy:** healthy canary over the observation window → `CANARY HEALTHY → promote-ready` (then a deliberate `make promote`) → `/tmp/qa/t30_healthy.txt`.
  - **QA failure:** inject a synthetic p95 spike (candidate `model_version`) → `ROLLBACK TRIGGERED` + canary removed → `/tmp/qa/t30_rollback.txt`; stop Prometheus 60s → conservative rollback → `/tmp/qa/t30_prom_down.txt`; force an unexpected check error (e.g., malformed query result) → fail-closed conservative rollback → `/tmp/qa/t30_failclosed.txt`.
  - **Commit:** `canary(rollback): canary_check.sh — model_version-filtered thresholds, fail-closed, 30s loop, promote-ready reporting (todo 30)`

- [x] 31. Model lifecycle end-to-end verification (train→eval→register→staging→canary→promote/rollback)
  - **References:** spec §M5 promotion flow; ties todos 6, 13, 27, 28, 29, 30. `scripts/demo_lifecycle.sh`; note code-version (git semver) vs model-version (registry) separation.
  - **Acceptance:** `demo_lifecycle.sh` happy path reaches canary promote-ready and then a deliberate `make promote` to prod on kind; a bad-model path is blocked at the eval gate OR auto-rolled-back at canary; both paths recorded.
  - **QA happy:** full lifecycle → promote-ready → deliberate `make promote` → `/tmp/qa/t31_lifecycle.txt`.
  - **QA failure:** bad model → blocked/rolled-back → `/tmp/qa/t31_lifecycle_fail.txt`.
  - **Commit:** `registry(lifecycle): end-to-end train→eval→register→canary→promote/rollback demo (todo 31)`

### Wave 7 — C6a: Observability (Milestone 7) (exit: Grafana dashboards render via API; an alert fires on a synthetic breach; traces→collector, logs→Loki)

- [ ] 32. Prometheus alert rules (latency, error-rate, GPU) + values.yaml
  - **References:** kube-prometheus-stack chart 87.19.0: `additionalPrometheusRulesMap` creates `PrometheusRule` CRs. **Oracle B2:** this todo EXTENDS the minimal `observability/prometheus/values.yaml` created in todo 19 (which already has the grafana sidecar + datasource + ServiceMonitor selection) by adding `additionalPrometheusRulesMap` (the 3 rule files) and applying via `helm upgrade`. `rules/latency-alerts.yaml` (`HighP95Latency`: `histogram_quantile(0.95, sum(rate(inferenceops_request_duration_seconds_bucket[5m])) by (le)) > 0.5`), `rules/error-rate-alerts.yaml` (5xx rate > 1% critical), `rules/gpu-alerts.yaml` (DCGM GPU util/mem thresholds; informational when no GPU).
  - **Acceptance:** `helm upgrade kube-prometheus … -f observability/prometheus/values.yaml` (extending the todo-19 release) applies; `kubectl get prometheusrules -n monitoring` lists the rules; a synthetic p95 breach fires `HighP95Latency` (visible via Prometheus `/api/v1/alerts`).
  - **QA happy:** rules loaded; synthetic breach → alert `firing` in `/api/v1/alerts` → `/tmp/qa/t32_alerts.txt`.
  - **QA failure:** malformed PromQL in a rule → Prometheus reports a rule-load error → `/tmp/qa/t32_alerts_fail.txt`.
  - **Commit:** `obs(prometheus): values.yaml + latency/error/gpu alert rules (load-verified) (todo 32)`

- [ ] 33. Grafana dashboards (4) — single JSON source, two provisioning wrappers
  - **References:** **Metis:** keep ONE JSON source reused by both the docker-compose file mount (todo 12) and the Helm path. Grafana subchart: inline `grafana.dashboards.<folder>.<name>.json` OR sidecar ConfigMap label `grafana_dashboard: "1"`; datasource uid `prometheus` (auto-created by the datasource sidecar). `observability/grafana/dashboards/{inference-overview,gpu-utilization,latency-breakdown,model-quality}.json`. **Verify via Grafana API (`/api/search`), not visually.** Dashboards: inference-overview (RPS, p50/p95/p99, error rate, replicas), gpu-utilization (DCGM util/mem, tokens/sec), latency-breakdown (queue/model-load/inference stages), model-quality (prediction/confidence distribution, drift score, eval accuracy, model version).
  - **Acceptance:** in BOTH compose and kind, Grafana `/api/search` returns all 4 dashboards; `/api/datasources` returns `prometheus`; every panel binds to datasource uid `prometheus` (no datasource-mismatch "no data").
  - **QA happy:** `curl grafana:3000/api/search` → 4 dashboards listed → `/tmp/qa/t33_dashboards.txt`.
  - **QA failure:** a panel references a wrong datasource uid → `/api/ds/query` errors for that panel → `/tmp/qa/t33_dashboards_fail.txt`.
  - **Commit:** `obs(grafana): 4 dashboards (single JSON source) + compose & Helm provisioning wrappers (todo 33)`

- [x] 34. OpenTelemetry collector + trace wiring + Loki log aggregation
  - **References:** `observability/opentelemetry/collector.yaml` (receivers `otlp`, processors `batch`, exporters `otlp`/`logging`/`prometheus`); deploy as a compose service + a k8s manifest. Wire gateway + model-server OTLP exporters (`OTEL_ENDPOINT`, todo 11 `telemetry.py`). `observability/loki/values.yaml` (loki-stack: loki + promtail scraping app logs).
  - **Acceptance:** a request through the gateway produces a trace span exported to the collector (collector metrics/logs show received spans); app logs are queryable in Loki (`logcli` query).
  - **QA happy:** send a request → collector reports a span received; Loki has matching log lines → `/tmp/qa/t34_otel.txt`.
  - **QA failure:** misconfigure `OTEL_ENDPOINT` → collector receives 0 spans (detected) → `/tmp/qa/t34_otel_fail.txt`.
  - **Commit:** `obs(otel/loki): OTel collector + trace wiring + Loki log aggregation (todo 34)`

- [x] 35. ML/drift metrics + worker drift_job + prediction/confidence instrumentation
  - **References:** spec §M7 ML metrics: prediction distribution, confidence distribution, drift score, accuracy on golden dataset, failed-eval count, model version in production. `apps/worker/src/drift_job.py`; extend `apps/model-server/src/metrics.py`. New metrics: `inferenceops_prediction_distribution` (Histogram of labels), `inferenceops_confidence_distribution` (Histogram of scores), `inferenceops_model_info` (gauge, labels `model_id`,`version`), `inferenceops_drift_score` (gauge). `drift_job.py` compares current prediction/feature distribution vs baseline (PSI/KS) → drift score; alert if > threshold.
  - **Acceptance:** predictions populate the prediction/confidence histograms; `drift_job` computes a drift score (~0 on in-distribution data, high on synthetic shifted data); `model_info` reflects the prod model version.
  - **QA happy:** serve N requests → histograms populated; `drift_job` on in-distribution data → low score → `/tmp/qa/t35_drift.txt`.
  - **QA failure:** feed a shifted feature distribution → high `drift_score` → drift alert → `/tmp/qa/t35_drift_fail.txt`.
  - **Commit:** `obs(ml-metrics): prediction/confidence histograms + worker drift_job + model_info gauge (todo 35)`

- [ ] 36. Incident runbook + sample postmortem + SLI/SLO definitions
  - **References:** spec §M7 + README (incident runbook, sample postmortem, SLI/SLO). `docs/incident-runbook.md`, referencing the real alert names (todo 32) + dashboards (todo 33) + the rollback procedure (todo 30).
  - **Acceptance:** `docs/incident-runbook.md` defines SLIs (p95 latency, error rate, throughput, GPU util) + SLOs (e.g., p95 < 500ms @ 99.5%, errors < 1%); every alert rule from todo 32 has a concrete response step + dashboard link; a blameless sample postmortem (latency-regression incident: timeline, root cause, action items) is included; SLOs are measurable from existing metrics.
  - **QA happy:** each alert name in `rules/` has a runbook entry (grep cross-check passes) → `/tmp/qa/t36_runbook.txt`.
  - **QA failure:** add a new alert with no runbook entry → the cross-check script flags it missing → `/tmp/qa/t36_runbook_fail.txt`.
  - **Commit:** `obs(docs): incident-runbook + sample postmortem + SLI/SLO definitions (todo 36)`

### Wave 8 — C6b: Load testing + benchmarking (Milestone 9) (exit: all 4 k6 scenarios run; benchmark table populated with real local numbers)

- [ ] 37. k6 smoke + stress tests + run_load_test.sh
  - **References:** `load_tests/k6/{smoke.js,stress.js}`; `scripts/run_load_test.sh` (parameterized: `SCENARIO`, `TARGET`, `DURATION`, `--out json=evals/reports/…`). k6 via devenv.
  - **Acceptance:** `smoke.js` passes against the kind/compose deployment (low RPS; checks `/v1/predict` 200 + p95 < threshold); `stress.js` ramps VUs past capacity and records max-RPS + error rate; results JSON is written.
  - **QA happy:** `make load-test-smoke` → all k6 checks pass → `/tmp/qa/t37_smoke.txt`.
  - **QA failure:** point at a down endpoint → smoke checks fail (non-zero exit) → `/tmp/qa/t37_smoke_fail.txt`.
  - **Commit:** `loadtest(k6): smoke + stress scenarios + parameterized run_load_test.sh (todo 37)`

- [ ] 38. k6 spike + soak tests
  - **References:** `load_tests/k6/{spike.js,soak.js}`. spike = idle → sudden high VUs → idle (triggers HPA; the k6 complement to todo 20's lightweight generator); soak = sustained moderate load over a configurable duration (detects leaks/restarts).
  - **Acceptance:** `spike.js` triggers HPA scale-up (correlate with `kubectl get hpa`) and recovers within SLO; `soak.js` runs the full duration with a stable latency trend and 0 unexpected container restarts.
  - **QA happy:** spike → HPA scales, post-recovery p95 within SLO → `/tmp/qa/t38_spike.txt`.
  - **QA failure:** soak against a deliberate memory-leak stub → latency grows / a restart is detected → `/tmp/qa/t38_soak_fail.txt`.
  - **Commit:** `loadtest(k6): spike (autoscaling trigger) + soak (stability/leak) scenarios (todo 38)`

- [ ] 39. Locust load test (user-requested)
  - **References:** spec §3 `load_tests/locust/locustfile.py` (explicitly in the user's repo structure). Locust as an alternative interactive load tool. Makefile target `load-test-locust` (headless: `locust -f locustfile.py --headless -u -r -t --host`).
  - **Acceptance:** `locust -f locustfile.py --headless -u 50 -r 10 -t 30s --host <TARGET>` completes and reports RPS/p50/p95/failures; a CSV/HTML report is generated.
  - **QA happy:** headless run → stats with 0% failures at moderate load → `/tmp/qa/t39_locust.txt`.
  - **QA failure:** target down → locust reports ~100% failures → `/tmp/qa/t39_locust_fail.txt`.
  - **Commit:** `loadtest(locust): locustfile + headless make target (todo 39)`

- [ ] 40. Benchmark report generator (real numbers)
  - **References:** spec §M9 benchmark table columns: scenario, replicas, RPS, p95, p99, error rate, cost-per-1k-requests, cold-start time, model-load time, GPU utilization, throughput-per-replica. `scripts/gen_benchmark.py` → `evals/reports/benchmark.md` + `.json`. Parse k6 JSON outputs + scrape Prometheus for replicas/GPU/cold-start/model-load-time; compute cost-per-1k from a cost model (CPU $/hr, GPU $/hr).
  - **Acceptance:** `benchmark.md` has a populated table with real local numbers for ≥ 3 scenarios (baseline CPU, HPA enabled, canary); the GPU-LLM row is marked `documented (no GPU)` when absent; cost-per-1k is computed (no silent NaN).
  - **QA happy:** `gen_benchmark.py` → table with ≥ 3 populated rows → `evals/reports/benchmark.md` + `/tmp/qa/t40_bench.txt`.
  - **QA failure:** a missing k6 output → the generator errors clearly (not silent NaN) → `/tmp/qa/t40_bench_fail.txt`.
  - **Commit:** `loadtest(benchmark): gen_benchmark.py → cost/latency/throughput table from real runs (todo 40)`

### Wave 9 — C6c: Advanced serving (Phase 4) (exit: vLLM/KServe manifests build; local `/v1/completions` works via llama.cpp on CPU; batch path works)

- [ ] 41. Worker batch inference + report generator
  - **References:** spec §3 `apps/worker/src/{batch_inference.py,report_generator.py}`; batch vs online inference (spec §MLOps). Makefile target `batch`.
  - **Acceptance:** `batch_inference.py` reads a dataset (jsonl/csv) from storage, runs the predictor in batches, writes a predictions jsonl; `report_generator.py` summarizes (counts, score distribution, latency) to a report md/json; both run WITHOUT the online server.
  - **QA happy:** batch over a 1000-row dataset → predictions file + report → `/tmp/qa/t41_batch.txt`.
  - **QA failure:** a malformed input row → batch logs + skips/reports the bad row and continues → `/tmp/qa/t41_batch_fail.txt`.
  - **Commit:** `serving(worker): batch_inference + report_generator (offline batch path) (todo 41)`

- [ ] 42. vLLM backend (GPU): Dockerfile + Helm values + OpenAI-compatible server docs (build/doc-verified)
  - **References:** vLLM v0.25.1: `vllm serve <model> --host 0.0.0.0 --port 8000 --dtype auto --max-model-len 4096 --gpu-memory-utilization 0.90 --tensor-parallel-size 1` (legacy `python -m vllm.entrypoints.openai.api_server --model …` also works); endpoints `/v1/chat/completions`, `/v1/completions`, `/v1/models`, `/v1/embeddings`; image `vllm/vllm-openai:latest` (run with `--ipc=host`); Helm chart lives **in-repo** at `examples/deployment/chart-helm` (name `chart-vllm`, NOT a hosted repo), `resources.limits."nvidia.com/gpu": 1`. CPU caveat: `VLLM_TARGET_DEVICE=cpu` + `vllm/vllm-openai-cpu` image, documented as "basic". `serving/vllm/{Dockerfile,values.yaml,openai_compatible_server.md}`. **Metis:** GPU-only → acceptance is build/config/doc, NOT "serves a request".
  - **Acceptance:** `hadolint serving/vllm/Dockerfile` clean (hadolint is in the devenv toolchain, todo 2; if a build test is desired, use the CPU image `vllm/vllm-openai-cpu` — the GPU `vllm/vllm-openai:latest` is multi-GB); `values.yaml` is valid YAML referencing `nvidia.com/gpu: 1`; `openai_compatible_server.md` has working curl examples for `/v1/completions` + `/v1/models`, the GPU prerequisite (NVIDIA device plugin + GPU node group), and the CPU fallback noted. Runtime serving is NOT required (no GPU).
  - **QA happy:** `hadolint` clean + `values.yaml` parses + doc curl examples present → `/tmp/qa/t42_vllm.txt`.
  - **QA failure:** `values.yaml` references a nonexistent GPU resource key → validation flags it → `/tmp/qa/t42_vllm_fail.txt`.
  - **Commit:** `serving(vllm): GPU Dockerfile + Helm values + OpenAI-compatible server docs (build/doc-verified) (todo 42)`

- [ ] 43. CPU-runnable LLM path via llama.cpp/ollama (OpenAI-compatible) + gateway LLM route
  - **References:** host has `llama-server` (llama.cpp, built-in OpenAI-compatible `/v1/chat/completions` + `/v1/completions`) and `ollama` (server not running, no models pulled). **Metis:** use `llama-server` with a SMALL model (download a tiny GGUF < ~1GB, or `ollama pull qwen2.5:0.5b`) + explicit server lifecycle + a timeout budget. `serving/llm-cpu/`; `scripts/run_llm_cpu.sh` (start `llama-server --alias small-model --port 8001` or `ollama serve`+`run`, wait for readiness); gateway routes `/v1/completions`, `/v1/chat/completions`, `/v1/models` → the LLM backend selected by `LLM_BACKEND` (`llama-cpu`/`ollama`/`vllm`) — reserved in todo 11.
  - **Acceptance:** `make llm-cpu-smoke` starts the CPU LLM (small model), the gateway `/v1/completions` returns a valid (non-empty) completion, `/v1/models` lists the model, then teardown is clean — all on CPU (no GPU).
  - **QA happy:** `llm-cpu-smoke` → `/v1/completions` returns completion JSON → `/tmp/qa/t43_llm.txt`.
  - **QA failure:** model not downloaded / server fails to start → readiness times out with a clear error → `/tmp/qa/t43_llm_fail.txt`.
  - **Commit:** `serving(llm-cpu): llama.cpp/ollama OpenAI-compatible CPU LLM + gateway LLM route + smoke (todo 43)`

- [ ] 44. KServe backend (Standard mode, no Istio): InferenceService + autoscaling + canary docs
  - **References:** KServe docs-stable 0.18 (latest release v0.19.0): `serving.kserve.io/deploymentMode: Standard` (`RawDeployment` is a deprecated alias), `serving.kserve.io/autoscalerClass: hpa`, `spec.predictor.scaleMetric: cpu|memory` + `scaleTarget`; **Standard mode = "No dependency on Knative or Istio"**; **Oracle B4:** `scripts/install_kserve.sh` FIRST installs cert-manager (pinned, e.g. `kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.3/cert-manager.yaml`, then `kubectl wait` for `cert-manager-webhook` Ready) — KServe's webhook needs it for serving certs and NO other todo installs cert-manager on kind — THEN installs KServe via `kubectl apply --server-side -f …/v0.18.0/kserve.yaml` + `kserve-cluster-resources.yaml` (K8s 1.32+). **`canaryTrafficPercent` is Serverless/Knative-only in 0.18 stable** (Standard-mode canary landed in 0.19+) → canary is documented, not functionally tested on kind. `serving/kserve/{inferenceservice.yaml,autoscaling.yaml,canary.yaml}`; `scripts/install_kserve.sh`.
  - **Acceptance:** `install_kserve.sh` installs cert-manager (pinned, webhook Ready) and then KServe in Standard mode (no Istio) on kind; a trivial sklearn `InferenceService` reaches `Ready`; `autoscaling.yaml` applies (a KServe-managed HPA is created); `canary.yaml` is documented with the Serverless-only constraint.
  - **QA happy:** ISVC `Ready=true`; `kubectl get hpa` shows the KServe-managed HPA → `/tmp/qa/t44_kserve.txt`.
  - **QA failure:** skip the cert-manager step → the KServe webhook fails (demonstrates why `install_kserve.sh` installs cert-manager first); `canaryTrafficPercent` in Standard mode is ignored (documented) → `/tmp/qa/t44_kserve_fail.txt`.
  - **Commit:** `serving(kserve): Standard-mode InferenceService + HPA autoscaling + canary docs (no Istio) (todo 44)`

- [ ] 45. Ray Serve scaffold + serving docs (fastapi benchmark/deployment)
  - **References:** spec §3 `serving/ray-serve/{serve_config.yaml,deployment.py}`. **Must-NOT-Have:** Ray Serve is **scaffold + documentation only** (no running implementation). `serving/fastapi/{benchmark.md,deployment.md}`; a `serving/README.md` tradeoff table (FastAPI vs vLLM vs KServe vs Ray Serve). Ray Serve autoscaling described via its queue-size monitoring (docs.ray.io), as comments/scaffold.
  - **Acceptance:** the ray-serve files exist as a clearly-marked documented scaffold (`python -m py_compile deployment.py` passes); `serving/fastapi/benchmark.md` links the todo-40 table; `serving/fastapi/deployment.md` is a complete kind/EKS guide; `serving/README.md` has the tradeoff table.
  - **QA happy:** ray-serve scaffold compiles + docs present → `/tmp/qa/t45_serving.txt`.
  - **QA failure:** scope guard — `grep` confirms NO running Ray deployment is wired into CI/compose (no `ray start`) → `/tmp/qa/t45_scope.txt`.
  - **Commit:** `serving(ray/docs): Ray Serve scaffold + doc (no impl) + fastapi benchmark/deployment docs (todo 45)`

### Wave 10 — Docs + README + polish (exit: README quickstart works from a clean checkout; all docs complete + cross-linked; failure-scenario suite passes)

- [ ] 46. README (full, per spec §6)
  - **References:** spec §6 README sections: What It Does, Architecture (the spec's ASCII diagram), Features, Quickstart, AWS Deployment, Benchmarks, Failure Scenarios Tested, Tradeoffs, Interview Deep Dives. (Quality is verified via the concrete proxies in the Acceptance — all §6 sections present, quickstart commands exit 0, internal link-check passes — not subjective prose.)
  - **Acceptance:** `README.md` has every spec §6 section; the quickstart commands (`make run-local`, `make deploy-local`, `make test`) actually work from a clean checkout; the todo-40 benchmark table is embedded; no broken internal links.
  - **QA happy:** internal link check (grep `](…)` targets exist) passes; README quickstart `make run-local` works → `/tmp/qa/t46_readme.txt`.
  - **QA failure:** a referenced doc path doesn't exist → the link-check flags it → `/tmp/qa/t46_readme_fail.txt`.
  - **Commit:** `docs(readme): full staff-grade README per spec §6 (todo 46)`

- [ ] 47. Docs: architecture + aws-deployment + kubernetes-ml-workloads
  - **References:** `docs/{architecture.md,aws-deployment.md,kubernetes-ml-workloads.md}` (the last finalizes todo 21). architecture = full explanation of the ASCII diagram + data flow + component responsibilities; aws-deployment = VPC/EKS/ECR/S3/IAM/IRSA/ALB walkthrough, `terraform plan`/`apply`, the offline-plan note (todo 24), IRSA pod permissions, GPU node group, cost.
  - **Acceptance:** all three docs are complete, internally consistent, and reference real manifest/Terraform paths; `aws-deployment.md` explains IRSA + the offline plan.
  - **QA happy:** docs reference existing files (path cross-check passes) → `/tmp/qa/t47_docs.txt`.
  - **QA failure:** a doc references a removed path → the cross-check flags it → `/tmp/qa/t47_docs_fail.txt`.
  - **Commit:** `docs: architecture + aws-deployment + kubernetes-ml-workloads (todo 47)`

- [ ] 48. Docs: autoscaling-strategy + observability + cost-analysis
  - **References:** `docs/{autoscaling-strategy.md,observability.md,cost-analysis.md}`. autoscaling-strategy = pod autoscaling (HPA/KEDA) vs node autoscaling (Karpenter/cluster-autoscaler), GPU autoscaling hardness, cold starts + model-load time, Karpenter EKS-only note; observability = Prometheus/Grafana/OTel/Loki/CloudWatch roles, SLI/SLO, dashboards + alerts, debugging a latency incident; cost-analysis = CPU vs GPU $/hr, cost-per-1k from todo 40, spot nodes, autoscaling savings.
  - **Acceptance:** all three complete; `cost-analysis.md` cites real benchmark numbers (todo 40); `autoscaling-strategy.md` distinguishes pod vs node scaling + GPU caveats.
  - **QA happy:** `cost-analysis.md` cites the benchmark table values → `/tmp/qa/t48_docs.txt`.
  - **QA failure:** `cost-analysis.md` with placeholder NaN → flagged → `/tmp/qa/t48_docs_fail.txt`.
  - **Commit:** `docs: autoscaling-strategy + observability + cost-analysis (todo 48)`

- [ ] 49. Failure-scenario test suite (executable where possible, doc-only for GPU/node)
  - **References:** spec README "Failure Scenarios Tested": model-server crash, bad model deploy, latency regression, traffic spike, GPU memory pressure, node failure. **Metis:** GPU-pressure + node-failure are NOT executable on single-node CPU kind → documented procedures; the other four are executable. `scripts/failure_scenarios/{crash_model_server.sh,bad_model_deploy.sh,latency_regression.sh,traffic_spike.sh}` + `{gpu_memory_pressure.md,node_failure.md}` + `run_failure_scenarios.sh`.
  - **Acceptance:** the 4 executable scenarios run on kind and behave per spec (crash → probe restart + alert; bad model → eval gate blocks / canary rollback; latency regression → rollback; spike → HPA) with evidence; GPU-pressure + node-failure are documented procedures clearly marked not-executable-here.
  - **QA happy:** `run_failure_scenarios.sh` → 4 executable PASS + 2 documented → `/tmp/qa/t49_failures.txt`.
  - **QA failure:** each executable scenario's recovery is verified (e.g., crash → the pod restarts, not stays down) → `/tmp/qa/t49_failures_detail.txt`.
  - **Commit:** `docs(qa): failure-scenario suite (4 executable + GPU/node documented) (todo 49)`

- [ ] 50. Docs: security + tradeoffs + interview-deep-dives + final consistency pass
  - **References:** `docs/{security.md,tradeoffs.md,interview-deep-dives.md}`; spec §7 interview topics (AWS, Kubernetes, MLOps, AI infrastructure). security = IAM/IRSA least-privilege, ECR scanning, no secrets in repo, non-root containers, network policy, API-key auth, private subnets; tradeoffs = EKS vs SageMaker, FastAPI vs KServe vs Ray Serve, HPA vs KEDA vs Karpenter, vLLM vs TGI vs Triton, canary vs blue-green, devenv vs raw flakes; interview-deep-dives = concise Q&A for every spec §7 bullet.
  - **Acceptance:** all three docs complete; `interview-deep-dives.md` covers every spec §7 bullet; the full `docs/` cross-link check passes; `make run-local && make deploy-local` are still green after all waves.
  - **QA happy:** `interview-deep-dives.md` has entries for all spec §7 topics (grep checklist) → `/tmp/qa/t50_interview.txt`.
  - **QA failure:** a spec §7 topic is missing → the checklist flags it → `/tmp/qa/t50_interview_fail.txt`.
  - **Commit:** `docs: security + tradeoffs + interview-deep-dives + final consistency pass (todo 50)`

## Final verification wave
- [ ] F1. Plan compliance audit
  - **References:** this plan's `## Todos` + `## Commit strategy`; `.omo/drafts/inferenceops-platform.md` (Components ledger, adopted decisions, Must-NOT-Have).
  - **Acceptance (read-only audit):** every implementation todo `1..N` has a corresponding commit and a recorded evidence path; dependency order respected (no wave started before its predecessor's exit check); no todo skipped or silently downgraded; every acceptance criterion met or explicitly waived with reason.
  - **QA:** happy — walk each todo, confirm commit + evidence artifact exists. failure — inject a fake "done" todo with no evidence; the audit must flag it as NON-COMPLIANT.
  - **Verdict:** APPROVE only if 100% of todos have commit + evidence + met acceptance.
- [ ] F2. Code quality review
  - **References:** `pyproject.toml` (ruff, basedpyright, pytest, coverage config), `apps/*/Dockerfile`, `infra/scripts/*.sh`, `.github/workflows/*.yml`.
  - **Acceptance:** `uv run ruff check .` clean; `uv run basedpyright` clean; coverage ≥80% model-server / ≥70% gateway; no secrets/keys in repo (`gitleaks`/grep clean); Dockerfiles multi-stage + non-root user + pinned base; `shellcheck infra/scripts/*.sh scripts/*.sh` clean; `actionlint .github/workflows/*.yml` clean.
  - **QA:** happy — all linters/typecheckers/coverage pass. failure — introduce a lint error + a hardcoded secret; both must be caught.
  - **Verdict:** APPROVE only if all gates green.
- [ ] F3. Real manual QA (hands-on, end-to-end on kind)
  - **References:** Makefile targets; `scripts/deploy_local.sh`; `load_tests/k6/spike.js`; `scripts/canary_check.sh`; docker-compose observability stack.
  - **Acceptance (actually run the system, record evidence):** `make run-local` → `curl :8080/v1/predict` valid JSON; `make deploy-local` → `kubectl get pods -n inferenceops` all Running; `make load-test-spike` → `kubectl get hpa -w` shows 2→5+ then scale-down; deploy a synthetic-bad canary → automatic rollback fires; Grafana dashboards render with data; benchmark table populated with real numbers; local `/v1/completions` via llama.cpp returns a completion.
  - **QA:** happy — full happy-path demo script runs green. failure — each of: crash the model server (probe restarts + alert), deploy bad model (eval gate blocks), latency regression (rollback), traffic spike (HPA), metrics outage (conservative rollback); each must behave per spec with recorded evidence.
  - **Verdict:** APPROVE only if the end-to-end demo + all 6 failure scenarios behave per spec with evidence.
- [ ] F4. Scope fidelity
  - **References:** `## Scope` IN + Must-NOT-Have; `.omo/drafts/inferenceops-platform.md` Components ledger.
  - **Acceptance:** every item in Scope IN is implemented; every Must-NOT-Have is respected (no training-from-scratch beyond the artifact script; no auto-`terraform apply`; no Istio; no Ray Serve *implementation* — scaffold/doc only; no scope reduction); each of the 6 components meets its exit check.
  - **QA:** happy — checklist of IN items all present. failure — verify no forbidden artifact exists (e.g., no service mesh dependency, no `terraform apply` in CI, no Ray deployment code).
  - **Verdict:** APPROVE only if IN-complete AND Must-NOT-Have-clean AND all 6 component exit checks pass.

## Commit strategy
- **Conventional Commits**, one commit per todo (or per tightly-coupled unit), each message prefixed with the todo number and a scope tag: `scaffold`, `server`, `gateway`, `ci`, `k8s`, `infra`, `registry`, `canary`, `obs`, `loadtest`, `serving`, `docs`.
  - Format: `<tag>(<area>): <imperative summary> (todo N)` — e.g. `server(model-server): add FastAPI predict/health/metrics endpoints (todo 9)`.
- **Atomic & green:** each commit leaves the tree passing its relevant gate (tests/lint/build) — no commit that breaks `make test` or `make ci-local`.
- **No secrets ever committed:** `.env` is gitignored; only `.env.example` is tracked; CI uses GitHub secrets/OIDC, never inline creds.
- **Signed-off** commits; the worker stages only the files named in each todo's References (planning-dirty-worktree aware — never touch unrelated/untracked paths).

## Success criteria
- All implementation todos `1..N` complete, each with commit + evidence path + met acceptance criteria.
- All six component exit checks (C1–C6, in `## Verification strategy`) pass on a clean checkout via `devenv shell`.
- `make ci-local` green; coverage ≥80% model-server / ≥70% gateway; `ruff` + `basedpyright` + `shellcheck` + `actionlint` clean.
- `terraform validate` succeeds offline for dev, staging, and prod; `terraform plan` succeeds offline against the local moto mock endpoint (todo 24) with the three EKS offline guards (`use_latest_ami_release_version=false`, `addons` omitted, `enable_irsa` off).
- `make deploy-local` brings the full stack up on kind; `make load-test-spike` demonstrates HPA autoscaling; a synthetic-bad canary triggers automatic rollback; Grafana dashboards render with live data.
- A benchmark table (scenario / replicas / RPS / p95 / p99 / error-rate / cost-per-1k / cold-start / model-load-time) is populated with real local numbers.
- README quickstart commands work from a clean checkout; `docs/` is complete (architecture, aws-deployment, kubernetes-ml-workloads, autoscaling-strategy, observability, incident-runbook, cost-analysis, security, tradeoffs, interview-deep-dives).
- Final verification wave F1–F4 all return APPROVE, results surfaced, and the user's explicit okay obtained before completion is declared.
