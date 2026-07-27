# Deployment guide: FastAPI model-server (kind + EKS)

How the InferenceOps model-server (`apps/model-server`, a FastAPI app serving
`/v1/predict` / `/v1/batch` on port 8080) is deployed to the two real targets:
a local **kind** cluster for development, and **EKS** for production. The canary
release flow that runs on top of either target is at the bottom.

The server loads a fraud-detection model into memory at startup and is stateful
in memory, so every Kubernetes decision (probes, rollout strategy, graceful
shutdown) is explained in [`docs/kubernetes-ml-workloads.md`](../../docs/kubernetes-ml-workloads.md).
This guide is the "how do I run it" companion; that doc is the "why is the
manifest shaped this way" reference.

| Target | Entry point | Overlay | Registry | GPU |
|---|---|---|---|---|
| kind (local dev) | `make deploy-local` | [`k8s/overlays/local`](../../k8s/overlays/local/) | none (`kind load`) | no |
| EKS (prod) | `terraform apply` in [`infra/terraform/envs/prod`](../../infra/terraform/envs/prod/) | [`k8s/overlays/prod`](../../k8s/overlays/prod/) | ECR | optional (g5.xlarge) |

---

## 1. Local deployment (kind)

One command brings up the full stack:

```bash
make deploy-local        # runs scripts/deploy_local.sh
```

[`scripts/deploy_local.sh`](../../scripts/deploy_local.sh) is idempotent and
runs these steps in order:

1. Build the two app images if missing (`inferenceops/model-server:dev` via
   `make docker-build`, `inferenceops/gateway:dev` via `docker build`).
2. Create the kind cluster `inferenceops` from
   [`infra/scripts/kind-config.yaml`](../../infra/scripts/kind-config.yaml)
   (skipped if it already exists). The config's `extraPortMappings` publish the
   nginx controller's hostPort as `localhost:8080` (HTTP) and `8443` (HTTPS).
3. Install the nginx ingress controller (the public edge; fronts the gateway).
4. Install kube-prometheus-stack into `monitoring` (the in-cluster Prometheus
   the custom-RPS HPA and canary checks depend on), using
   [`observability/prometheus/values.yaml`](../../observability/prometheus/values.yaml)
   when present.
5. `kind load docker-image` both images onto the node. There is **no registry**,
   so the local overlay sets `imagePullPolicy: Never`.
6. Apply the local overlay [`k8s/overlays/local`](../../k8s/overlays/local/).
7. Set the gateway API-key Secret and roll the gateway to pick it up.

The local overlay ([`k8s/overlays/local/kustomization.yaml`](../../k8s/overlays/local/kustomization.yaml))
layers on top of [`k8s/base`](../../k8s/base/):

- `patch-model-server.yaml`: `imagePullPolicy: Never`, 2 replicas.
- `patch-hpa-custom-rps.yaml`: appends the external `inferenceops_rps` metric
  (averageValue 30 per replica) to the base cpu/memory HPA.
- `servicemonitor-model-server.yaml`: tells Prometheus to scrape `/metrics`,
  which feeds that external metric.

### Verify the local stack

```bash
scripts/verify_k8s.sh    # 15 live assertions, exit 0 iff all PASS

curl -s http://localhost:8080/healthz -H 'Host: inferenceops.local'
curl -s -X POST http://localhost:8080/v1/predict \
  -H 'Host: inferenceops.local' -H 'Content-Type: application/json' \
  -H 'X-API-Key: test-key' \
  -d '{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'
```

The ingress host is `inferenceops.local`, so every request needs the `Host`
header. `X-API-Key: test-key` is the local dev key the deploy script writes.
Teardown: `kind delete cluster --name inferenceops`.

---

## 2. Production deployment (EKS)

Production runs on EKS, provisioned by the Terraform roots under
[`infra/terraform/envs/`](../../infra/terraform/envs/) (`dev`, `staging`,
`prod`). Each env wires the same seven modules from
[`infra/terraform/modules/`](../../infra/terraform/modules/):

| Module | Role |
|---|---|
| `vpc` | Network: public + private subnets across AZs. |
| `ecr` | Container registry for the `:prod` images. |
| `eks` | The cluster: a CPU node group (always) + an optional GPU node group + optional Karpenter. |
| `iam` | The IRSA role the model-server pod assumes. |
| `s3` | The model-artifacts bucket the server reads from. |
| `alb` | The L7 edge in front of the service. |
| `monitoring` | CloudWatch alarms (ALB-backed). |

### 2a. Provision the cluster

```bash
cd infra/terraform/envs/prod
cp terraform.tfvars.example terraform.tfvars   # offline_plan=false, enable_gpu_node_group=true
terraform init
terraform plan -var offline_plan=false         # needs real AWS credentials
terraform apply -var offline_plan=false
```

`offline_plan=false` is the real-AWS path: it restores IRSA/OIDC, real AMI
resolution, and the core EKS addons (coredns, kube-proxy, vpc-cni). Running it
with no credentials fails auth on purpose; that failure is the guard proving
this is the real path. For a credential-free rehearsal, `scripts/tf_mock.sh
start` runs a moto endpoint and `terraform plan` (default `offline_plan=true`)
succeeds offline. See [`infra/terraform/envs/prod/README.md`](../../infra/terraform/envs/prod/README.md).

### 2b. IRSA (IAM Roles for Service Accounts)

The model-server reads its model from S3. Instead of static AWS keys, the pod
assumes an IAM role via IRSA:

- The `iam` module ([`infra/terraform/modules/iam/main.tf`](../../infra/terraform/modules/iam/main.tf))
  creates the role `${name}-model-server-irsa`, trusts the cluster OIDC provider
  for the subject `system:serviceaccount:inferenceops:model-server`, and attaches
  `AmazonS3ReadOnlyAccess`. `enable_irsa=true` (the real-AWS path) is what
  provisions the OIDC provider the trust relationship needs.
- The pod runs under the `model-server` ServiceAccount
  ([`k8s/base/serviceaccount.yaml`](../../k8s/base/serviceaccount.yaml)). To
  complete the binding, annotate that ServiceAccount with the role ARN (the
  `iam` module's `role_arn` output):

  ```bash
  kubectl -n inferenceops annotate serviceaccount model-server \
    eks.amazonaws.com/role-arn="$(terraform -chdir=infra/terraform/envs/prod output -raw model_server_irsa_role_arn)"
  ```

  The base ServiceAccount ships without the annotation (the local/kind path has
  no OIDC provider); add it in the prod overlay or at deploy time as above.

### 2c. GPU node group (optional)

The default fraud model is CPU-only, so the base manifest requests no GPU. The
prod path documents the GPU shape end to end:

- The `eks` module's optional GPU node group
  ([`infra/terraform/modules/eks/main.tf`](../../infra/terraform/modules/eks/main.tf)),
  gated by `enable_gpu_node_group` (prod tfvars set it `true`): `g5.xlarge`,
  `ami_type = BOTTLEROCKET_x86_64_NVIDIA`, tainted
  `nvidia.com/gpu=present:NoSchedule` and labeled `nvidia.com/gpu.present=true`.
- The prod overlay ([`k8s/overlays/prod/patch-model-server.yaml`](../../k8s/overlays/prod/patch-model-server.yaml))
  makes the Deployment GPU-ready: `nodeSelector: nvidia.com/gpu.present=true`
  pins it onto GPU nodes, and a toleration for the `nvidia.com/gpu=present:NoSchedule`
  taint lets it schedule there. To actually bind a GPU, uncomment
  `nvidia.com/gpu: 1` in BOTH requests and limits (a GPU request must equal its
  limit). The taint keeps cheaper CPU workloads off the GPU nodes.
- Karpenter (gated by `enable_karpenter`) provisions exactly-sized nodes from
  the cloud API and is EKS-only. See [`docs/kubernetes-ml-workloads.md`](../../docs/kubernetes-ml-workloads.md) §7.

For a GPU LLM workload specifically, the serving backend is vLLM
([`serving/vllm/`](../vllm/)), not the FastAPI server.

### 2d. Push images and apply the prod overlay

CI pushes the `:prod` images to ECR (`.github/workflows/build-and-push.yml`).
Then apply the prod overlay against the EKS context:

```bash
aws eks update-kubeconfig --name inferenceops-prod
kubectl apply -k k8s/overlays/prod
kubectl -n inferenceops rollout status deployment/model-server
```

The prod overlay ([`k8s/overlays/prod/kustomization.yaml`](../../k8s/overlays/prod/kustomization.yaml))
retags both images to `:prod` (`imagePullPolicy: IfNotPresent` against ECR),
runs 3 model-server replicas, bumps CPU/memory, and upgrades the base soft
pod anti-affinity to a hard hostname-spread rule.

---

## 3. Manifests reference (`k8s/base/`)

| File | Object | Role |
|---|---|---|
| `namespace.yaml` | Namespace `inferenceops` | Boundary for all app objects. |
| `deployment-model-server.yaml` | Deployment `model-server` | The FastAPI server: readiness `/readyz`, liveness `/healthz`, `maxUnavailable: 0` rollout, preStop drain. |
| `deployment-gateway.yaml` | Deployment `gateway` | Auth + rate-limit edge in front of the server. |
| `service-model-server.yaml` | Service `service-model-server:8080` | ClusterIP; the gateway calls it. Not exposed at the edge. |
| `service-gateway.yaml` | Service `service-gateway:8080` | ClusterIP fronted by the ingress. |
| `ingress.yaml` | Ingress `inferenceops` | nginx, host `inferenceops.local`, `path / -> service-gateway`. |
| `hpa.yaml` | HPA `model-server` | cpu + memory (the local overlay adds the external RPS metric). |
| `pdb.yaml` | PodDisruptionBudget | `minAvailable: 1`. |
| `serviceaccount.yaml` | ServiceAccounts `model-server` + `gateway` | Per-app identity (IRSA binds to `model-server`). |
| `configmap.yaml` | `inferenceops-config` | `MODEL_ID`, `MODEL_VERSION`, `RATE_LIMIT_RPS`, `LOG_LEVEL`. |
| `secret.example.yaml` | `gateway-api-keys` | Placeholder dev API keys; real values stay out of git. |
| `otel-collector.yaml` | OTel collector | Trace/metric pipeline (todo 34). |

Every hardening field above is asserted live by [`scripts/verify_k8s.sh`](../../scripts/verify_k8s.sh).

---

## 4. Canary release (nginx canary-weight split)

How a candidate model version rolls out behind an nginx canary-weight split
(`make deploy-canary` / `make promote` / `make rollback`, todo 29), with the
automated health-driven rollback (todo 30) built on top.

### Request topology

**Base path** (steady state):

```
client -> ingress(inferenceops) -> service-gateway -> service-model-server
              host: inferenceops.local   (auth + rate-limit)   (model_version=1.0.0)
```

The base ingress (`k8s/base/ingress.yaml`, object name `inferenceops`) fronts
the gateway, which authenticates (`X-API-Key`), rate-limits, and proxies to the
model-server.

**Canary path** (during the canary window): the canary `stable-ingress`
**replaces** the base ingress for host `inferenceops.local` (same object name
`inferenceops`, so `kubectl apply` overwrites it in place). The two ingresses do
not coexist. The split happens at the model-server tier:

```
                                 +-(90%)-> service-model-server          (stable,    1.0.0)
client -> nginx (host inferenceops.local)
                                 +-(10%)-> service-model-server-canary   (candidate, 2.0.0)
        stable-ingress  (inferenceops)         -> service-model-server
        canary-ingress  (inferenceops-canary)  -> service-model-server-canary
                          nginx.ingress.kubernetes.io/canary: "true"
                          nginx.ingress.kubernetes.io/canary-weight: "10"
```

The gateway is bypassed during the canary window so the 90/10 split happens
cleanly at the model-server tier. Consequence: `/v1/predict` needs NO
`X-API-Key` while the canary is live (auth/rate-limit live in the gateway).
`make rollback` reinstates the base ingress and restores auth.

Candidate vs stable traffic is discriminated by the `model_version`
metric/response label: the canary pods run `MODEL_VERSION=2.0.0` and stamp it
on every `/v1/predict` response and every `inferenceops_*` metric, so the split
is observable in the response and in Prometheus.

### The candidate artifact

The canary loads a second model version so its `model_version` label differs
from stable. `models/artifacts/fraud_detector_canary/` carries `model_meta.json`
with `model_id: fraud-detector`, `model_version: 2.0.0` (plus a `model.pkl`, a
copy of the stable model for the routing demo; a real canary would carry a
genuinely retrained artifact). Resolution by `LocalStorageBackend` is
metadata-driven (it matches the `model_id`/`model_version` in `model_meta.json`,
not the directory name). Model versioning is decoupled from git semver.

Because the canary pod runs the same image as stable
(`inferenceops/model-server:dev`) and the committed Dockerfile bakes in only the
stable artifact, `scripts/deploy_canary.sh` augments the image at deploy time:
it builds `FROM` the current dev image (by immutable ID) with one extra
`COPY models/artifacts/fraud_detector_canary/ ...`, re-tags as
`inferenceops/model-server:dev`, and `kind load`s it. Both artifacts then
resolve from one image: stable pods (env `MODEL_VERSION=1.0.0`) load 1.0.0,
canary pods (env `MODEL_VERSION=2.0.0`) load 2.0.0. The committed
`apps/model-server/Dockerfile` is never modified.

### Canary manifests (`k8s/canary/`)

| File | Object | Role |
|---|---|---|
| `canary-deployment.yaml` | Deployment `model-server-canary` + Service `service-model-server-canary` | Candidate tier; `MODEL_VERSION=2.0.0`, `imagePullPolicy: Never`, readiness `/readyz`, liveness `/healthz`. 1 replica (the split is by ingress weight, not replica count). |
| `stable-ingress.yaml` | Ingress `inferenceops` | Replaces the base ingress for the host; `-> service-model-server` (stable 90%). |
| `canary-ingress.yaml` | Ingress `inferenceops-canary` | Same host; `canary: "true"` + `canary-weight: "10"`; `-> service-model-server-canary` (candidate 10%). `ingressClassName: nginx`. |

Both ingresses share host `inferenceops.local` and `ingressClassName: nginx`,
which the controller needs to merge the canary into the main server block and
split by weight.

### Lifecycle

```bash
# 90/10 split (candidate at 10%). Bakes the candidate artifact into the image,
# kind-loads it, applies the canary tier + both ingresses, swaps the base ingress.
make deploy-canary MODEL_VERSION=2.0.0

# 100% candidate. Stops the automated check loop first (todo 30), then raises
# canary-weight to 100. Deliberate gated step, never auto-promoted.
make promote

# 0% candidate. Deletes the canary Ingress/Deployment/Service and restores the
# base ingress (-> service-gateway; auth/rate-limit back). Idempotent.
make rollback
```

All three are idempotent and safe to re-run; `rollback` converges to the clean
stable state from any canary state (@10%, promoted @100%, or partially torn
down).

### Observing the split

The `model_version` is in the `/v1/predict` response body, so the split is
countable per-request (no auth needed during the canary):

```bash
for i in $(seq 1 100); do
  curl -s -X POST http://localhost:8080/v1/predict \
    -H 'Host: inferenceops.local' -H 'Content-Type: application/json' \
    -d '{"features":[0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1]}'
  echo
done | sort | uniq -c   # ~90% model_version 1.0.0, ~10% 2.0.0
```

or via the `inferenceops_request_count{model_version=...}` metric in Prometheus.
The ~10% figure is statistical (nginx rounds weight per-request); assert a range
(e.g. 3-20%), not exactly 10%.

### Automated rollback (todo 30)

`scripts/deploy_canary.sh` starts a `canary_check.sh` loop on a 30s interval
that compares candidate-vs-stable p95 / error-rate / eval score (filtered by the
`model_version` label) and writes a `promote-ready` marker after a clean window;
on breach it runs `make rollback`. The loop is terminal (exits on promote-ready)
and `make promote` stops it first (kills the loop PID at
`${TMPDIR:-/tmp}/inferenceops-canary-check.pid` and clears the marker) so a
post-promote check cannot race the promotion or roll back a just-promoted
version. Any unexpected check error fails closed to rollback.

---

## See also

- [`benchmark.md`](./benchmark.md): FastAPI serving performance (links the todo-40 benchmark table).
- [`docs/kubernetes-ml-workloads.md`](../../docs/kubernetes-ml-workloads.md): the manifest-by-manifest "why" behind every field above.
- [`docs/autoscaling-strategy.md`](../../docs/autoscaling-strategy.md): HPA vs KEDA vs Karpenter, and the external-RPS metric.
- [`infra/terraform/envs/prod/README.md`](../../infra/terraform/envs/prod/README.md): the real-AWS vs offline Terraform paths.
- [`serving/README.md`](../README.md): FastAPI vs vLLM vs KServe vs Ray Serve tradeoffs.
