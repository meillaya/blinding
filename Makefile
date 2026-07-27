# InferenceOps — operator interface.
#
# Self-documenting: `make` (or `make help`) lists every target with its
# description, parsed from the `## ` comments below.
#
# Docker/Kind run rootless on a user socket, so point DOCKER_HOST at it unless
# the caller already exported one.
export DOCKER_HOST ?= unix:///run/user/1000/docker-me.sock

# Image tag used by the docker-build target (override with IMAGE=...).
IMAGE ?= inferenceops/model-server:dev

# Ingress URL the locust load test targets (override with LOCUST_HOST=...).
LOCUST_HOST ?= http://localhost:8080

# `make` with no arguments shows the help.
.DEFAULT_GOAL := help

.PHONY: help run-local test lint typecheck coverage-gate docker-build ci-local \
	deploy-local deploy-staging \
	load-test-smoke load-test-spike load-test-soak load-test-stress load-test-locust \
	deploy-canary promote rollback \
	eval train llm-cpu-smoke canary-check batch

help: ## Show available targets and their descriptions
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

## Local development
run-local: ## Run the model server locally on :8080 (uvicorn)
	cd apps/model-server && PYTHONPATH=src uv run uvicorn inferenceops_model_server.server:app --host 0.0.0.0 --port 8080

## Quality gates
test: ## Run the test suite with pytest (with coverage)
	uv run pytest

lint: ## Lint the codebase with ruff
	uv run ruff check .

typecheck: ## Type-check the codebase with basedpyright
	uv run basedpyright

coverage-gate: ## Enforce per-package coverage gates (model-server >=80%, gateway >=70%)
	uv run python scripts/coverage_gate.py

docker-build: ## Build the model-server container image (rootless docker)
	docker build -t $(IMAGE) -f apps/model-server/Dockerfile .

ci-local: ## Run the full local CI pipeline (lint, typecheck, test, docker-build)
	$(MAKE) lint typecheck test docker-build

## Deployment
deploy-local: ## Deploy the full stack to a local kind cluster
	scripts/deploy_local.sh

deploy-staging: ## Deploy the platform to the staging environment
	scripts/deploy_staging.sh

## Load testing
load-test-smoke: ## k6 smoke load test (minimal traffic, sanity; writes evals/reports/k6-smoke.json)
	scripts/run_load_test.sh

load-test-spike: ## Spike load test — bounded curl burst that exercises HPA/KEDA scale-up (no k6 needed)
	scripts/spike_load.sh

load-test-soak: ## k6 soak load test (sustained moderate traffic; writes evals/reports/k6-soak.json)
	SCENARIO=soak scripts/run_load_test.sh

load-test-stress: ## k6 stress load test (push past capacity; writes evals/reports/k6-stress.json)
	SCENARIO=stress scripts/run_load_test.sh

load-test-locust: ## Locust headless load test (50 users/30s; writes evals/reports/locust CSV + HTML)
	mkdir -p evals/reports
	uv run --with locust locust -f load_tests/locust/locustfile.py --headless \
		-u 50 -r 10 -t 30s --host $(LOCUST_HOST) \
		--csv=evals/reports/locust --html=evals/reports/locust.html

## Canary / release lifecycle
deploy-canary: ## Deploy a canary model version (usage: make deploy-canary MODEL_VERSION=v4)
	scripts/deploy_canary.sh $(MODEL_VERSION)

promote: ## Promote the canary to 100% (deliberate gated step)
	scripts/promote.sh

rollback: ## Roll back the canary to 0% (stable only)
	scripts/rollback.sh

canary-check: ## Run the automated canary health check (p95/errors/eval, fail-closed)
	scripts/canary_check.sh

## Modeling
eval: ## Run all evaluation gates (accuracy, latency, regression)
	scripts/run_evals.sh

train: ## Train the fraud-detection model and write the artifact + metrics
	uv run python models/training/train.py

batch: ## Run a batch inference job
	scripts/batch.sh

## LLM
llm-cpu-smoke: ## Smoke-test the small CPU LLM end-to-end via the gateway
	scripts/llm_cpu_smoke.sh
