# Audit of the InferenceOps demo

The old tree is at commit `1b3af72`. Paths in this file refer to that commit;
`git show 1b3af72:<path>` still reaches every one. The replacement is Blinding,
a local llama.cpp lab that records what it measures.

## What I ran

The old repo was checked out clean in a rootless podman container with the
workspace mounted. The container had Python 3.12, uv, and the repo's own lock
file.

- `uv run pytest --collect-only` reported 260 tests.
- `uv run pytest` passed all 260 in 43 seconds.
- `make run-local` started uvicorn, printed "Application startup complete", and
  served requests with no model artifact present. The readiness endpoint could
  not become ready without one.
- `make train` wrote `models/artifacts/fraud_detector/model.pkl` and its metadata.
- `make eval` passed every gate and printed accuracy 0.9655, F1 0.6102, ROC AUC
  0.9184 against the baseline.
- After training, `make run-local` returned 200 from `/readyz` and the
  documented predict call returned `score=0.034`.

The tests were real. The problem is what they and the documentation measured.

## Findings

### 1. The model and its evaluation are generated from the same distribution

`models/training/sklearn_bridge.py:53` calls `sklearn.datasets.make_classification`
to build the fraud data. `evals/eval_common.py:5` says the golden dataset is the
held-out validation split of the same generator. The reported accuracy and ROC
AUC measure whether a classifier can separate two synthetic Gaussian clusters.
They say nothing about fraud, and the "golden" set cannot catch a change in the
world because it has never seen the world.

### 2. The benchmark table reports load shedding as throughput

`evals/reports/benchmark.json` contains a spike row with 2,897.5 RPS and a 93.04%
error rate. `README.md:301` presents it as a real local number. The load test
sends malformed requests on purpose and the gateway sheds most of a burst, so
the requests that "count" are mostly errors. The cost column divides a node price
by that error-inflated rate and produces $0.0002 per 1k.

`scripts/gen_benchmark.py:43` defines `NO_GPU = "documented (no GPU)"` and writes
that string into numeric columns. `README.md:300` through `README.md:305` call the
result a real benchmark.

### 3. The quickstart cannot work from a clean clone

`.gitignore:19` excludes `models/artifacts/**/*.pkl`. No model file is tracked.
`README.md:199` says `make run-local` is the first command, then shows a predict
call. A clean clone has no artifact to load, and the README never says to run
`make train` first. I reproduced the failure: the server starts, but it cannot
serve the documented response.

### 4. The table cannot be recomputed from the repo

`evals/reports/` tracks `baseline.json`, `benchmark.json`, and `benchmark.md`.
The k6 and locust files those summaries claim to parse are git-ignored. There is
no run ID, no host line, no commit, and no seed in the reported rows. The table
is an assertion with a generated timestamp.

### 5. The cloud, GPU, and serving layers are unverified

`infra/terraform/README.md:25` says the Terraform surface was validated offline
against moto. Nothing was applied. EKS, ECR, ALB, Karpenter, KServe, vLLM, Loki,
and DCGM appear in `README.md` and `docs/` as implemented or measured.
`docs/cost-analysis.md:42` repeats `documented (no GPU)` in a table that calls
itself measured. None of these claims can be checked on this machine, and the
repo does not contain a run that checks them.

### 6. The prose is written for a hiring loop

`README.md:392` is a repository layout, `README.md:407` is a list of links to more
documentation, `README.md:416` is a stale note about those links, and
`README.md:423` is a license section. The docs include `interview-deep-dives.md`
and phrases like "staff candidate". The text tells a reader how to be impressed
rather than what the code does.

### 7. The operational claims need a cluster and untracked artifacts

The canary check, eval gate, registry, and load tests require a running kind
cluster, Prometheus, and model artifacts that are not in the repo. CI runs the
Python units only. A reader cannot reproduce the release, rollback, or
autoscaling behavior from a clone, so those claims are stories about a past
session.

## What I kept

The gateway's auth, rate limiter, proxy error mapping, and tests were real and
run on every request. The llama.cpp CPU path was the one piece that produced a
model that could be served and measured without a cloud account. The prose and
drawing rules were already written down and useful.

## What replaced it

The demo platform is gone. Blinding is one Python package with a CLI. It pins
model files by commit and SHA-256, serves them with llama.cpp on the CPU or a
HIP build on the GPU, measures TTFT and inter-token latency from streamed
responses, runs llama-bench sweeps, scores a prompt suite, fine-tunes the tiny
F32 test model, and writes a run record for every command.

The gateway stays as an optional front door with API keys, a token bucket, and
Prometheus metrics. The cloud, kind, canary, Terraform, and interview layers are
removed.

## How the new claims are checked

Every row in the README points at `results/<run-id>/run.json`. The record carries
the git commit, the host and device, the llama.cpp versions, the model revision
and SHA-256, the config, the workload, the metrics, and a validity verdict. A run
with too many errors or too few samples is marked invalid and does not enter the
table. A benchmark on a dirty tree is recorded as dirty and kept out of the
README.

The commands in the README were copied from a sandbox session that ran. The
sandbox is a rootless podman container with a read-only root, a workspace-only
bind mount, a workspace podman store, and only `/dev/kfd` and the render node
passed through. It is removed after the final check.

## What is still not measured

The GPU rows depend on the ROCm container opening the RX 7800 XT. If the device
gate fails, Blinding reports CPU only and the GPU code is removed rather than
described. Fine-tuning is llama.cpp's work-in-progress example on the 1.2 MB test
model. That proves the harness, not the quality of a model. No cloud deployment
is claimed, because none was tested.

## Remaining risks

The CPU numbers move with other work on the host. Load average is recorded and
every tuning candidate repeats, but the README is a snapshot, not an SLO.
llama.cpp changes its flags between releases, so the pinned build is part of the
record. The upstream finetune example may stop working; the command will be cut
if it does.
