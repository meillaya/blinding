# Interview deep dives

Concise Q&A for the topics a staff candidate should be able to defend, grouped
the way the spec groups them: AWS, Kubernetes, MLOps, and AI infrastructure.
Every answer is grounded in something this repo actually does, with a link to
the code, manifest, or doc that proves it. The README's Interview Deep Dives
section is the short version; this page is the full set.

## AWS

### Q1. Why IRSA instead of static AWS keys, and how does the binding actually work?

Static keys are long-lived, shared, and end up in a Secret some pod can read.
IRSA (IAM Roles for Service Accounts) gives each pod short-lived credentials
scoped to its service account, with nothing stored in the cluster.

In this repo the [`iam` module](../infra/terraform/modules/iam/) creates one
role, `${name}-model-server-irsa`, whose OIDC trust condition names exactly one
subject: `system:serviceaccount:inferenceops:model-server`. The only attached
policy is `AmazonS3ReadOnlyAccess`. So only model-server pods, in that
namespace, can assume a role that can only read S3. The gateway and worker get
no AWS access at all. The binding completes at deploy time by annotating the
service account with the role ARN; the base
[`serviceaccount.yaml`](../k8s/base/serviceaccount.yaml) carries no annotation
because kind has no OIDC provider. Full flow:
[`aws-deployment.md` section 5](./aws-deployment.md#5-irsa-how-the-model-server-pod-gets-s3-access).

### Q2. How do you `terraform plan` EKS without AWS credentials?

You cannot serve every data source from a mock, so the plan is shaped to avoid
the ones a mock cannot answer. CI runs a local moto server
([`scripts/tf_mock.sh`](../scripts/tf_mock.sh)) and points the provider at it;
moto answers STS and EC2 data sources. Three guards in
[`envs/prod/main.tf`](../infra/terraform/envs/prod/main.tf) remove the rest:
`use_latest_ami_release_version=false` (avoids the SSM AMI lookup, which lives
under a reserved namespace moto rejects), `addons={}` (avoids the
addon-version lookup), and `enable_irsa=false` (avoids the live TLS fetch of
the cluster OIDC certificate). With those off, `terraform plan` exits 0
offline; `offline_plan=false` restores all three and is the real-AWS path.
Detail: [`aws-deployment.md` section 3](./aws-deployment.md#3-offline-terraform-plan-no-credentials).

### Q3. Walk me through the VPC design.

Three AZs, one public and one private subnet each
([`vpc` module](../infra/terraform/modules/vpc/)). EKS nodes live in the
private subnets, which have no internet route. Egress goes through one
NAT gateway per AZ (HA, so losing one AZ does not strand the others'
ECR pulls and S3 reads). The public subnets carry `kubernetes.io/role/elb=1` and the private
ones `kubernetes.io/role/internal-elb=1`, which is how the AWS Load Balancer
Controller discovers where to put internet-facing versus internal load
balancers. The ALB sits in the public subnets with a security group that opens
only the listener port; everything else stays private. See
[`aws-deployment.md` section 1](./aws-deployment.md).

### Q4. How is the container supply chain protected?

Four layers. (1) Images are multi-stage, non-root (uid 1000), and pinned to
`python:3.12-slim` plus a pinned uv, so the runtime carries no build tooling.
(2) [`build-and-push.yml`](../.github/workflows/build-and-push.yml) runs Trivy
and fails the build on HIGH/CRITICAL CVEs before any push. (3) The
[`ecr` module](../infra/terraform/modules/ecr/) sets `scan_on_push = true`
(every push is CVE-scanned) and `image_tag_mutability = "IMMUTABLE"` (a tag
always resolves to one digest). (4) The push itself is gated on `main` plus the
presence of OIDC secrets, and CI authenticates to AWS with `id-token: write`
and a role, never a static key. Forks and PRs build and scan but cannot
publish. The full picture is in [`docs/security.md`](./security.md).

### Q5. EKS or SageMaker?

EKS, for control. The same kustomize base deploys to kind locally and to EKS
on AWS, the canary is an nginx annotation you can read, and the autoscaling
signals are explicit. SageMaker would hide exactly the layer this project
exists to show, at the price of AWS lock-in. The full argument is in
[`docs/tradeoffs.md` section 1](./tradeoffs.md#1-eks-vs-sagemaker).

## Kubernetes

### Q6. Why scale on an external RPS metric instead of CPU?

Because CPU lies here. The gateway sheds about 95% of a vertical burst, so
model-server CPU stayed around 3% even at the spike's ~3700 RPS, while the HPA
scaled 2→7 off the external `inferenceops_rps` metric (averageValue 30 per
replica). A CPU-only HPA would have under-reacted to real demand. The metric
path is gateway → Prometheus scrape → prometheus-adapter → HPA, which carries
a 30-50s lag; the spike test holds 75s so the scale-up materializes inside the
window. CPU and memory stay as co-signals. Full treatment:
[`docs/autoscaling-strategy.md`](./autoscaling-strategy.md).

### Q7. How does the canary work, and why fail-closed?

nginx-ingress splits by annotation: a canary Ingress with `canary-weight: 10`
sends 10% to the candidate ([`k8s/canary/`](../k8s/canary/)). The
`model_version` metric label is the discriminator that lets
[`canary_check.sh`](../scripts/canary_check.sh) compare candidate against
stable on the same Prometheus. Every 30s it rolls back on candidate p95 above
stable × 1.20, error ratio above 1%, or eval regression above 3%. Fail-closed
means any check error, or Prometheus down longer than 60s, also rolls back:
the system never promotes on data it cannot trust, even if that occasionally
rolls back a healthy canary when metrics blip. Promotion itself stays a
deliberate `make promote`, never automatic.

### Q8. Why a canary instead of blue-green?

Blue-green flips 100% of traffic at once; the new version only ever saw
mirrored or synthetic load before it served everything. A canary proves the
candidate on a 10% slice of real traffic first, so the blast radius of a bad
deploy is 10% until the check rolls it back. That matters here because the
rollback decision uses live per-version signals (p95, error ratio, eval
quality) that only real traffic produces. Blue-green still wins for stateful
schema migrations where two versions cannot share a database; the full
comparison is in [`docs/tradeoffs.md` section 5](./tradeoffs.md#5-canary-vs-blue-green).

### Q9. Cold start versus model-load time. Why does the distinction matter?

They are different clocks with different owners. Model-load time is the
in-process cost of deserializing weights, measured by
`inferenceops_model_load_seconds` (about 0.84s here). Cold start is the whole
pod path, schedule + boot + load, measured from the pod's own conditions
(about 7s on kind). It matters because autoscaling latency is dominated by
cold start, not model load, and a crash-looping model shows up as a rising
model-load metric long before probes catch it. Both numbers are real and come
from [`docs/autoscaling-strategy.md` section 4](./autoscaling-strategy.md#4-cold-starts-and-model-load-time).

### Q10. What do the probes and shutdown path look like?

`/readyz` returns 503 until the model is loaded, so a pod takes traffic only
when it can actually serve; `/healthz` is the liveness signal and is exempt
from API-key auth so the kubelet needs no credentials. Shutdown is graceful:
preStop plus `terminationGracePeriodSeconds` give in-flight requests time to
drain, uvicorn handles SIGTERM (it runs as PID 1 via exec-form CMD), and the
[`kubernetes-ml-workloads` doc](./kubernetes-ml-workloads.md) walks the rolling
update, PDB, and anti-affinity choices around it.

### Q11. HPA, KEDA, or Karpenter?

Different layers. HPA scales pods (this repo's default, on external RPS plus
cpu/memory). KEDA also scales pods but from event sources the HPA cannot see
(queue depth, lag, cron); it is installed here as the documented alternative,
and the two share one external-metrics APIService, so you run exactly one.
Karpenter scales nodes, provisioning right-sized machines when pods go
Pending; it is EKS-only (kind has no cloud provider) and lives in the
Terraform. Detail in
[`docs/autoscaling-strategy.md`](./autoscaling-strategy.md) and
[`kubernetes-ml-workloads.md` section 7](./kubernetes-ml-workloads.md#7-hpa-vs-cluster-autoscaler-vs-karpenter).

### Q12. What changes between the kind cluster and EKS?

Four things. Credentials: kind has none, EKS uses IRSA (Q1). Node scaling:
Karpenter needs a cloud provider, so it is Terraform-only on kind. Images:
kind has no registry, so images are `kind load`-ed and pods use
`imagePullPolicy: Never`, while EKS pulls from ECR by digest. Ports: rootless
kind maps high host ports (8080→80), which is why `make run-local` and the
cluster cannot both bind host :8080. The manifests themselves do not change;
that is the point of the kustomize overlay layout.

## MLOps

### Q13. How does the model registry version things?

Versioning is deliberately decoupled from both git and the weights. The registry
([`models/registry/`](../models/registry/)) treats the model version as an
opaque label: `register_model.py` records it, `promote_model.py` moves it
toward production, and `approve_model.py` gates the promotion on evals. A model
version (say `2.0.0`) is not a git tag and not a commit; the eval gate
validates the weights, and the label just names them. That separation is what
lets the canary compare two versions by the `model_version` metric label
without the registry knowing anything about Kubernetes.

### Q14. What do the eval gates actually check?

Three gates run in CI ([`model-eval-gate.yml`](../.github/workflows/model-eval-gate.yml))
and again at the canary. Accuracy: the golden-dataset score must clear a floor
(≥0.85). Latency: inference must stay inside a budget. Regression: the new
weights must not drop more than 3% against the baseline on f1. The regression
gate is the subtle one; a model can pass accuracy at 0.9475 yet collapse on f1,
and that is exactly the degraded model the failure-scenario suite uses to prove
the gate blocks a bad deploy before it ever reaches the cluster.

### Q15. What is the artifact-identity invariant, and why the upload order?

A model resolves by `(model_id, version)`, and the stored metadata IS the
resolution key; if the metadata's recorded identity does not match the upload
arguments, the storage layer raises instead of publishing an artifact that
resolves under the wrong name. Uploads write `model.pkl` first and
`model_meta.json` last, because the metadata publishes the artifact to
resolution. A partial upload therefore leaves bytes invisible (never a phantom
model), and a re-run completes it idempotently. One `ModelStorage` interface
serves `models/artifacts` locally and `s3://` on EKS through IRSA, so the
invariant holds on both backends.

### Q16. How do you detect drift, and why is the baseline exactly zero?

The worker's drift job
([`apps/worker/src/inferenceops_worker/drift_job.py`](../apps/worker/src/inferenceops_worker/drift_job.py))
computes the population stability index (PSI) of scored traffic against the
training distribution and exports `inferenceops_drift_score`. The bin shares
are clipped to an epsilon (1e-6) before the PSI sum, and clipping preserves
equality: identical distributions score a literal 0.0, which gives tests and QA
a clean deterministic in-distribution baseline. A 3-sigma shift across the 20
features scores about 7.4 against a 0.2 threshold. Exit codes carry the
verdict: 0 stable, 1 drift alert, 2 operational error.

### Q17. How are SLOs defined so they never disagree with the alerts?

By reusing the alert expressions verbatim. The latency SLO IS the
`HighP95Latency` expression held under 0.5 per 5m window (99.5% of windows over
30 days, about 216 minutes of error budget); the error SLO is the
`HighErrorRate` ratio under 0.01; throughput is a capacity number tested
offline by k6. Because the SLO and the alert compute the same PromQL, they
cannot drift apart, and the runbook maps each alert to a diagnosis. See
[`docs/observability.md`](./observability.md) and the
[`incident runbook`](./incident-runbook.md#highp95latency-p95-inference-latency-above-500-ms).

### Q18. Online versus batch inference. Why two paths?

They share a model but not a clock. Online serving runs the `DynamicBatcher`,
which coalesces live requests up to a max batch or wait time and fans results
back under a latency budget. Batch work
([`apps/worker/src/inferenceops_worker/batch_inference.py`](../apps/worker/src/inferenceops_worker/batch_inference.py))
mirrors it offline: one vectorized `predict`/`predict_proba` per 100-row batch,
malformed rows skipped with a line-numbered reason into a metadata sidecar, and
a parity test that locks batched results equal to rowwise predictions. Same
weights, one interface, two latency regimes.

### Q19. Why devenv for the toolchain instead of raw Nix flakes?

Because the goal is a reproducible developer environment, not a bespoke package
expression. [`devenv.nix`](../devenv.nix) (with `devenv.yaml` and a lock file)
declares the tools the host lacks (kind, k6, helm, kustomize, uv, a pinned
Python 3.12, shellcheck, actionlint, hadolint) in one high-level file, and
`devenv shell` materializes them. It wraps a flake underneath, so the escape
hatch to raw nixpkgs stays open without writing a `devShells` output by hand.
The payoff is that every script in the repo runs identically on a fresh
checkout. Raw flakes win only when you need an overlay story a higher-level
tool would fight; see [`docs/tradeoffs.md` section 6](./tradeoffs.md#6-devenv-vs-raw-nix-flakes).

## AI infrastructure

### Q20. How do you choose a serving backend?

By what the model actually is. A scikit-learn classifier on CPU with a custom
schema gets a small FastAPI app: minimal moving parts, fully benchmarked. A
high-throughput LLM on GPU gets vLLM (PagedAttention, continuous batching,
OpenAI-compatible). A fleet of models gets KServe's InferenceService contract.
A model that is really a DAG gets Ray Serve's queue-size autoscaler. This repo
implements FastAPI, documents and installs KServe, documents vLLM, and
scaffolds Ray Serve, so the choice is explicit and reversible. The comparison
table is in [`serving/README.md`](../serving/README.md).

### Q21. vLLM versus TGI versus Triton?

vLLM for OpenAI-shaped LLM throughput (chosen here, pinned at
`vllm/vllm-openai:v0.25.1`, a drop-in for the gateway's reserved routes). TGI
when your models and tooling are already Hugging Face-native. Triton when you
serve a heterogeneous fleet (TensorRT, ONNX, PyTorch) or need server-side
ensembles, at the cost of a model repository and per-backend configuration.
Full argument in [`docs/tradeoffs.md` section 4](./tradeoffs.md#4-vllm-vs-tgi-vs-triton).

### Q22. Why is GPU serving hard? Why can't you reuse the CPU playbook?

Four reasons, detailed in
[`docs/autoscaling-strategy.md` section 5](./autoscaling-strategy.md#5-gpu-autoscaling-hardness).
(1) The bottleneck is VRAM, not CPU, so you must scale on DCGM co-metrics
(utilization and framebuffer memory), which the alert rules already carry.
(2) Node provisioning is slow and expensive; a GPU node takes minutes and
costs about $1.212/hr, so over-provisioning hurts far more than on CPU.
(3) Cold starts are dominated by multi-gigabyte weight loads, not the 0.84s
model load of the fraud model. (4) The runtime has constraints CPU serving
lacks: vLLM needs an 8Gi `/dev/shm` (or `--ipc=host`) or torch crashes under
load, plus a device plugin and runtime class. This host has no GPU, so the
GPU-pressure and node-failure scenarios are documented procedures, not
executable tests ([`scripts/failure_scenarios/`](../scripts/failure_scenarios/)).

### Q23. How do you reason about the cost of serving?

With one formula and real numbers. `cost/1k = replicas × node_$hr / (RPS ×
3600) × 1000`, billing one full node per replica (conservative), at $0.384/hr
for a CPU m6i.2xlarge and $1.212/hr for a GPU g5.2xlarge. The measured baseline
is $0.0058 per 1k requests; static provisioning at peak would cost about 3×
that, which is the autoscaling saving. The canary row honestly shows $0.0390
per 1k, an artifact of billing a whole node against a 10% traffic slice, and
the doc names it rather than hiding it. All of it is in
[`docs/cost-analysis.md`](./cost-analysis.md#cost-model) and the
[`benchmark report`](../evals/reports/benchmark.md).

### Q24. How do the OpenAI-compatible LLM routes work without a GPU?

A small CPU LLM stands in. llama-server (llama.cpp) serves
`/v1/models`, `/v1/completions`, and `/v1/chat/completions` for a quantized
0.5B model at about 4 tok/s, which is plenty to prove the contract. The gateway
holds a second HTTP client for the LLM upstream with its own timeout and error
label, and `/v1/models` ownership switches so exactly one route ever serves it.
With no LLM configured the routes return 503, not a crash. The real target is
vLLM on a `g5` node; the CPU path keeps the routes runnable and smoke-tested on
a laptop.

### Q25. KServe: why Standard mode, and what does the canary limitation teach you?

Standard mode (Knative serving, no Istio) keeps the mesh out of a project that
deliberately runs no service mesh; two InferenceServices go Ready on the kind
cluster, each with a KServe-managed HPA. The lesson is in
[`serving/kserve/canary.yaml`](../serving/kserve/canary.yaml): on KServe 0.18
Standard mode, `canaryTrafficPercent` is admitted by the webhook but inert.
The ISVC goes Ready, yet exactly one predictor Deployment exists and
`status.traffic` stays empty, because percentage splitting is Knative-revision
routing that Standard mode does not do (it landed in 0.19+). Knowing which
features a mode actually honors, versus merely accepts, is the difference
between a demo and a deployment.

## See also

- [`docs/security.md`](./security.md): IRSA, secrets, and network isolation in full.
- [`docs/tradeoffs.md`](./tradeoffs.md): the six tradeoff arguments behind the choices above.
- [`docs/autoscaling-strategy.md`](./autoscaling-strategy.md): pod vs node scaling, GPU caveats.
- [`docs/observability.md`](./observability.md) and [`docs/incident-runbook.md`](./incident-runbook.md): the SLO and alert story.
- [`serving/README.md`](../serving/README.md): the serving-backend comparison table.
