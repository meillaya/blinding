# Blinding operator interface.
#
# `make` lists every target with its description. The default model is the
# smallest one in the manifest so a first run does not pull seven gigabytes.

MODEL ?= qwen2.5-0.5b-instruct:q4_k_m
CONFIG ?= configs/local-cpu.toml
CORPUS ?= data/corpus.txt
UV ?= uv
SANDBOX ?= scripts/sandbox.sh

.DEFAULT_GOAL := help

.PHONY: help sync test lint typecheck prose check figures \
	doctor models serve bench tune eval train kernel results \
	sandbox-init sandbox-cpu sandbox-rocm sandbox-check sandbox-clean clean

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync: ## Install the package and its development dependencies
	$(UV) sync

test: ## Run the test suite
	$(UV) run pytest

lint: ## Lint with ruff
	$(UV) run ruff check .

typecheck: ## Type check with basedpyright
	$(UV) run basedpyright

prose: ## Check the house prose rules
	$(UV) run python tools/check_prose.py

check: lint typecheck test prose ## Run every gate

figures: ## Regenerate the Excalidraw scenes and their PNGs
	$(UV) run python docs/figures/build.py
	scripts/render_figures.sh

doctor: ## Print host, tool, and device facts
	$(UV) run blinding doctor

models: ## List the pinned models
	$(UV) run blinding models list

serve: ## Serve the default model (MODEL=... CONFIG=...)
	$(UV) run blinding serve $(MODEL) --config $(CONFIG)

bench: ## Benchmark the default model (MODEL=... CONFIG=...)
	$(UV) run blinding bench $(MODEL) --config $(CONFIG)

tune: ## Sweep configurations for the default model
	$(UV) run blinding tune $(MODEL)

eval: ## Score the prompt suite on the default model
	$(UV) run blinding eval $(MODEL) --config $(CONFIG)

train: ## Fine-tune the tiny F32 model (CORPUS=...)
	$(UV) run blinding train stories260k-f32 --corpus $(CORPUS)

kernel: ## Run the HIP copy and GEMM benchmark (GPU image only)
	$(UV) run blinding kernel

results: ## List recorded runs
	$(UV) run blinding results list

sandbox-init: ## Create the workspace-local podman store
	$(SANDBOX) init

sandbox-cpu: ## Build the CPU image
	$(SANDBOX) build --cpu

sandbox-rocm: ## Build the ROCm image
	$(SANDBOX) build --rocm

sandbox-check: ## Run every gate inside the CPU image
	$(SANDBOX) run --cpu make check

sandbox-clean: ## Remove the workspace podman store and caches
	$(SANDBOX) clean

clean: sandbox-clean ## Remove local caches
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
