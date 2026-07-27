# AWS deployment

> **Optional.** This entire AWS/Terraform layer is optional. The core platform
> runs fully on a local kind cluster with no AWS account, credentials, or spend.
> Nothing in the local platform depends on anything described here. For the
> local-only path, see the [README Quickstart](../README.md#quickstart) and
> [serving/fastapi/deployment.md](../serving/fastapi/deployment.md).

How the InferenceOps platform lands on AWS: the Terraform modules under
[infra/terraform/modules/](../infra/terraform/modules/), the environment roots
under [infra/terraform/envs/](../infra/terraform/envs/), the credential-free
offline `terraform plan`, IRSA pod permissions, the optional GPU node group,
and cost. The Kubernetes side of the prod path (manifest fields, GPU
scheduling) is in [kubernetes-ml-workloads.md](kubernetes-ml-workloads.md);
the end-to-end "run it" guide is in
[serving/fastapi/deployment.md](../serving/fastapi/deployment.md).

One rule up front: **`terraform apply` is a manual step.** Nothing in this
repo (no CI workflow, no script) ever runs `apply`. CI and local rehearsal
only ever run `plan`.

---

## 1. Topology

```
   internet
      │
      ▼
   ┌─────┐   PUBLIC subnets  10.0.1.0/24, 10.0.2.0/24, 10.0.3.0/24   (3 AZ)
   │ ALB │   tag kubernetes.io/role/elb=1
   └──┬──┘   listener :80 ──▶ target group :8080 (target_type=ip, health /healthz)
      │
      │        VPC 10.0.0.0/16  (DNS hostnames + support, NAT GW per AZ)
      ▼
   ┌────────────────────────────────────────────────────────────┐
   │  EKS cluster "inferenceops-prod"  (Kubernetes 1.33)        │
   │  PRIVATE subnets  10.0.11.0/24, 10.0.12.0/24, 10.0.13.0/24 │
   │                                                            │
   │  • cpu node group   m6i.xlarge   min 2 / desired 2 / max 6 │
   │  • gpu node group   g5.xlarge    min 0 / desired 0 / max 4 │
   │    (optional; taint nvidia.com/gpu=present:NoSchedule)     │
   │  • Karpenter submodule (optional; EKS Pod Identity)        │
   └───────────────┬───────────────────────────┬────────────────┘
                   │ IRSA (OIDC trust)         │ image pull via NAT GW
                   ▼                           ▼
   ┌────────────────────────────┐   ┌──────────────────────────────┐
   │ IAM role                   │   │ ECR  inferenceops-prod-{gate…│
   │ *-model-server-irsa        │   │      model-server, worker}   │
   │ AmazonS3ReadOnlyAccess     │   │ IMMUTABLE tags + scan-on-push│
   └─────────────┬──────────────┘   └──────────────────────────────┘
                 ▼
   ┌────────────────────────────┐   ┌──────────────────────────────┐
   │ S3 inferenceops-prod-model…│   │ CloudWatch (monitoring modu…│
   │ versioned, SSE-AES256,     │   │ log groups gateway / model-  │
   │ all public access blocked  │   │ server / worker; ALB 5xx +   │
   └────────────────────────────┘   │ p95 alarms ──▶ SNS (option… │
                                    └──────────────────────────────┘
```

---

## 2. Module walkthrough

The env roots ([dev](../infra/terraform/envs/dev/), [staging](../infra/terraform/envs/staging/),
[prod](../infra/terraform/envs/prod/)) all wire the same seven modules. The prod
root is [infra/terraform/envs/prod/main.tf](../infra/terraform/envs/prod/main.tf).

### vpc → [infra/terraform/modules/vpc/](../infra/terraform/modules/vpc/)

Wraps `terraform-aws-modules/vpc/aws ~> 6.6`. Public + private subnets across
three AZs (`us-east-1a/b/c` by default), one NAT gateway per AZ (HA egress so
private-subnet nodes can reach ECR and S3), DNS hostnames + resolution on
(required by EKS node private DNS), and the AWS Load Balancer Controller
discovery tags: public subnets `kubernetes.io/role/elb=1`, private subnets
`kubernetes.io/role/internal-elb=1`. Outputs `vpc_id`, `private_subnets`
(EKS), `public_subnets` (ALB).

### eks → [infra/terraform/modules/eks/](../infra/terraform/modules/eks/)

Wraps `terraform-aws-modules/eks/aws ~> 21.24` (v21 names: `name`,
`kubernetes_version`, `addons`).

- **CPU node group (always):** `m6i.xlarge`, min 2 / desired 2 / max 6.
- **GPU node group (optional,** gated by `enable_gpu_node_group`, default
  `false`): `g5.xlarge`, `ami_type = BOTTLEROCKET_x86_64_NVIDIA`, min 0 /
  desired 0 / max 4, tainted `nvidia.com/gpu=present:NO_SCHEDULE` so only
  GPU-tolerant workloads schedule there. See §6.
- **Karpenter (optional,** gated by `enable_karpenter`): the EKS module's
  `//modules/karpenter` submodule with `create_pod_identity_association = true`
  (v21 dropped the Karpenter IRSA variables in favor of EKS Pod Identity; the
  standalone Karpenter repo is gone). Karpenter provisions right-sized nodes
  directly from the EC2 API when pods go `Pending`. It is EKS-only and is
  documented in [autoscaling-strategy.md](autoscaling-strategy.md).

### ecr → [infra/terraform/modules/ecr/](../infra/terraform/modules/ecr/)

One repository per shipped image: `gateway`, `model-server`, `worker`
(namespaced per env, e.g. `inferenceops-prod-gateway`). Hardened defaults:
`image_tag_mutability = IMMUTABLE` (a deployed tag always resolves to one
digest, so deploys are reproducible) and `scan_on_push = true` (CVE scan on
every push).

### s3 → [infra/terraform/modules/s3/](../infra/terraform/modules/s3/)

The model-artifacts bucket (`inferenceops-prod-model-artifacts`), wrapping
`terraform-aws-modules/s3-bucket/aws ~> 5.15`: versioning enabled (every
artifact write is recoverable), SSE AES256 at rest (S3-managed keys, no KMS
dependency), and all four public-access blocks on. Weights are never served
from S3 directly; the model-server fetches them over IRSA-scoped reads.
`force_destroy` is off in prod, so a non-empty bucket cannot be destroyed by
accident (dev/staging allow it).

### iam → [infra/terraform/modules/iam/](../infra/terraform/modules/iam/)

The IRSA role. Wraps `terraform-aws-modules/iam/aws//modules/iam-role ~> 6.6`
with `enable_oidc = true` (v6 removed `iam-assumable-role-with-oidc` and
merged it into `iam-role`). Inputs from the prod root:

```hcl
role_name         = "inferenceops-prod-model-server-irsa"
oidc_provider_url = module.eks.oidc_provider          # issuer URL, no https://
oidc_subjects     = ["system:serviceaccount:inferenceops:model-server"]
policies          = { S3ReadOnly = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess" }
```

The resulting trust relationship: the role is assumable **only** by the
`model-server` ServiceAccount in the `inferenceops` namespace of this cluster.
Full IRSA explanation in §5.

### alb → [infra/terraform/modules/alb/](../infra/terraform/modules/alb/)

Plain resources (`aws_lb` + `aws_lb_target_group` + `aws_lb_listener` + a
security group), deliberately unwrapped so the module has zero data sources
and plans identically against real AWS and the offline mock. Internet-facing,
in the public subnets, listener on `:80` forwarding to IP targets on `:8080`
(the model-server pods, registered by the AWS Load Balancer Controller in the
real-AWS path), target-group health check on `/healthz`.

### monitoring → [infra/terraform/modules/monitoring/](../infra/terraform/modules/monitoring/)

Also plain resources: one CloudWatch log group per component
(`/inferenceops/<name>/{gateway,model-server,worker}`, 14-day retention), an
optional SNS email topic (`alarm_email`), and two SLO alarms on the ALB:
`HTTPCode_ELB_5XX_Count` (Sum) and `TargetResponseTime` p95. Both use
`treat_missing_data = "notBreaching"` so an idle environment never pages.
This is the AWS-side baseline; the in-cluster Prometheus/Grafana/Loki stack
(see [architecture.md](architecture.md) §4) is the primary observability path.

---

## 3. Offline `terraform plan` (no credentials)

The env roots support a fully offline `terraform plan` against a local
[moto](https://github.com/getmoto/moto) mock endpoint. It exists so the whole
Terraform surface is exercisable in CI and on a laptop with no AWS account.

```bash
scripts/tf_mock.sh start          # moto_server on http://localhost:5000
cd infra/terraform/envs/prod
terraform init
terraform plan                    # offline_plan=true is the default
# … Plan: 82 to add, 0 to change, 0 to destroy.
scripts/tf_mock.sh stop
```

[scripts/tf_mock.sh](../scripts/tf_mock.sh) is idempotent (`start` / `stop` /
`status` / `restart`) and runs moto from the repo venv (`uv run python -m
moto.server`).

Three pieces make the offline plan succeed, all gated by `var.offline_plan`
(default `true`):

1. **The provider** ([infra/terraform/envs/prod/providers.tf](../infra/terraform/envs/prod/providers.tf)):
   a `dynamic "endpoints"` block points `sts, iam, ec2, eks, s3, elbv2,
   cloudwatch, logs, ssm` at `http://localhost:5000`, plus
   `skip_credentials_validation`, `skip_requesting_account_id`,
   `skip_metadata_api_check`, static `mock`/`mock` keys, and path-style S3
   addressing (virtual-hosted DNS cannot resolve against localhost).
2. **moto answers the mockable data sources:** `aws_caller_identity` /
   `aws_iam_session_context` (STS) and `aws_subnet` / availability zones
   (EC2). `aws_partition` / `aws_region` resolve locally with no API call.
3. **The three EKS offline guards** ([infra/terraform/envs/prod/main.tf](../infra/terraform/envs/prod/main.tf))
   avoid the data sources moto *cannot* serve:

   | Guard (when `offline_plan=true`) | Data source it removes | Why moto can't serve it |
   |---|---|---|
   | `use_latest_ami_release_version = false` | `data "aws_ssm_parameter"` | The EKS-optimized AMI lives under the reserved `/aws/` SSM namespace; moto rejects writes there, so it cannot be seeded |
   | `addons = {}` | `data "aws_eks_addon_version"` (read per addon) | moto 404s `DescribeAddonVersions` |
   | `enable_irsa = false` | `data "tls_certificate"` | A live HTTPS fetch of the cluster OIDC issuer cert, impossible before the cluster exists |

The skip flags only suppress the provider's own init calls; data sources are
still read at plan time, which is exactly why the three guards are needed on
top of the mock endpoint.

**This is a plan-only rehearsal.** `apply` against moto would create nothing
real, and is never run. The offline plan's job is to prove the configuration
is coherent (resource graph, counts, no schema errors) with zero cloud
access.

---

## 4. Real-AWS path (plan + apply)

```bash
cd infra/terraform/envs/prod
cp terraform.tfvars.example terraform.tfvars    # offline_plan=false, enable_gpu_node_group=true
terraform init
terraform plan  -var offline_plan=false         # needs real credentials (AWS_PROFILE / env keys)
terraform apply -var offline_plan=false         # MANUAL. Deliberately. Never automated.
```

[infra/terraform/envs/prod/terraform.tfvars.example](../infra/terraform/envs/prod/terraform.tfvars.example)
documents the intended prod shape: `offline_plan = false` and
`enable_gpu_node_group = true`. With `offline_plan=false` the provider
collapses to a normal one (default credential chain, real endpoints, no
skips), and the three guards flip back: real AMI resolution, the core addons
(`coredns`, `kube-proxy`, `vpc-cni`), and IRSA/OIDC.

The guard that proves this is the real path: running `offline_plan=false`
with **no** credentials fails auth. That failure is expected and is the
evidence the code path genuinely reaches AWS.

`apply` stays manual by design. A human reads the plan, confirms the resource
count and the destroy-risk resources, and runs `apply` themselves. The same
holds for `dev` and `staging` (their roots carry the identical offline
machinery; staging abbreviates its name to `inferenceops-stg` to stay inside
the IAM 38-character role-name-prefix limit).

---

## 5. IRSA: how the model-server pod gets S3 access

The model-server reads model artifacts from S3. Instead of static AWS keys
mounted into the pod, it assumes an IAM role through **IRSA** (IAM Roles for
Service Accounts). The trust chain:

```
  model-server pod
   └─ runs as ServiceAccount "model-server" (namespace inferenceops)
       └─ SA annotated with eks.amazonaws.com/role-arn = <irsa role ARN>
           └─ EKS projects a signed OIDC service-account token into the pod
               └─ pod calls STS AssumeRoleWithWebIdentity
                   └─ STS validates the token against the cluster OIDC issuer
                       └─ role trust policy matches the subject
                          system:serviceaccount:inferenceops:model-server
                           └─ pod gets temporary S3-read credentials
                              (AmazonS3ReadOnlyAccess)
```

The pieces, concretely:

1. **The OIDC provider** exists only on the real-AWS path: `enable_irsa = true`
   (which `offline_plan=false` sets) is what makes the EKS module create the
   IAM OIDC provider for the cluster. The offline plan turns it off (no
   cluster issuer to fetch a cert from), so the offline plan creates no IRSA
   wiring. That is correct: there is no cluster yet.
2. **The role** ([infra/terraform/modules/iam/main.tf](../infra/terraform/modules/iam/main.tf)):
   `inferenceops-prod-model-server-irsa`, trusting the cluster OIDC provider
   for the single subject `system:serviceaccount:inferenceops:model-server`,
   with `AmazonS3ReadOnlyAccess` attached. Read-only is the whole policy: the
   pod can fetch artifacts and nothing else. Writes go through the upload
   workflow (`scripts/upload_model.sh`, CI with its own OIDC role), never
   from the serving pod.
3. **The binding** is a deploy-time annotation on the ServiceAccount
   ([k8s/base/serviceaccount.yaml](../k8s/base/serviceaccount.yaml)). The base
   manifest ships **without** the annotation (kind has no OIDC provider); add
   it in prod:

   ```bash
   kubectl -n inferenceops annotate serviceaccount model-server \
     eks.amazonaws.com/role-arn="$(terraform -chdir=infra/terraform/envs/prod output -raw model_server_irsa_role_arn)"
   ```

   The role ARN is the `model_server_irsa_role_arn` output
   ([infra/terraform/envs/prod/outputs.tf](../infra/terraform/envs/prod/outputs.tf)).
   Once annotated, the AWS SDK in the pod (the `S3StorageBackend` in
   `apps/model-server/src/inferenceops_model_server/storage/`) picks up the
   projected token automatically. No keys, no Secret, no env vars.

---

## 6. GPU node group (optional)

The default fraud model is CPU-only, so the GPU group is off by default and
the base manifests request no GPU. Prod enables it
(`enable_gpu_node_group = true` in the tfvars) to be GPU-ready for LLM
backends such as vLLM ([serving/vllm/](../serving/vllm/)).

The node side ([infra/terraform/modules/eks/main.tf](../infra/terraform/modules/eks/main.tf)):

```hcl
gpu = {
  name           = "inferenceops-prod-gpu"
  instance_types = ["g5.xlarge"]                 # NVIDIA A10G
  ami_type       = "BOTTLEROCKET_x86_64_NVIDIA"
  min_size       = 0, desired_size = 0, max_size = 4
  taints = { gpu = { key = "nvidia.com/gpu", value = "present", effect = "NO_SCHEDULE" } }
}
```

The workload side ([k8s/overlays/prod/patch-model-server.yaml](../k8s/overlays/prod/patch-model-server.yaml))
matches it exactly:

```yaml
nodeSelector:
  nvidia.com/gpu.present: "true"      # pin to GPU nodes
tolerations:
  - key: nvidia.com/gpu               # tolerate the node-group taint
    operator: Equal
    value: present
    effect: NoSchedule
```

The composition does three things at once: the **taint** repels every
CPU-only workload from expensive GPU nodes; the **toleration** lets the
model-server schedule there; the **nodeSelector** pins it there. To actually
*bind* a GPU device, uncomment `nvidia.com/gpu: 1` in **both** requests and
limits in the prod patch (a GPU request must equal its limit, and GPUs are
not fractionally shared by default). The NVIDIA device plugin that advertises
the `nvidia.com/gpu` resource installs via
[infra/scripts/install_gpu_plugin.sh](../infra/scripts/install_gpu_plugin.sh).

The min/desired-0 sizing means the GPU group costs nothing at idle; the node
autoscaler (Karpenter, §2) or an ASG scale-up adds nodes when GPU pods go
`Pending`. GPU scheduling mechanics in
[kubernetes-ml-workloads.md](kubernetes-ml-workloads.md) §10.

---

## 7. Deploying the workloads after `apply`

Terraform creates the cluster and its surroundings; the workloads still deploy
through kubectl + kustomize:

```bash
aws eks update-kubeconfig --name inferenceops-prod
kubectl apply -k k8s/overlays/prod
kubectl -n inferenceops annotate serviceaccount model-server \
  eks.amazonaws.com/role-arn="$(terraform -chdir=infra/terraform/envs/prod output -raw model_server_irsa_role_arn)"
kubectl -n inferenceops rollout status deployment/model-server
```

The prod overlay ([k8s/overlays/prod/kustomization.yaml](../k8s/overlays/prod/kustomization.yaml))
retags both images to `:prod` (pulled from ECR with `imagePullPolicy:
IfNotPresent`), runs 3 model-server replicas, bumps resources
(requests 4/8Gi, limits 8/16Gi), adds the GPU nodeSelector + toleration, and
upgrades the base soft anti-affinity to a hard hostname-spread rule
([k8s/overlays/prod/patch-model-server-antiaffinity.yaml](../k8s/overlays/prod/patch-model-server-antiaffinity.yaml)).
CI pushes the `:prod` images to ECR via
[.github/workflows/build-and-push.yml](../.github/workflows/build-and-push.yml).

Operational note: the GPU nodeSelector pins model-server to GPU nodes, so the
prod overlay assumes the GPU node group is enabled. For a CPU-only prod
deployment, drop the nodeSelector/toleration from the patch (or use the
staging overlay).

---

## 8. Cost

The two node groups frame the cost decision: a CPU `m6i.xlarge` (4 vCPU / 16
GiB) versus a GPU `g5.xlarge` (NVIDIA A10G). The benchmark's cost model bills
one full node per replica (conservative) at us-east-1 on-demand rates, CPU
$0.384/hr (m6i.2xlarge) and GPU $1.212/hr (g5.2xlarge), and derives a
cost-per-1k-requests figure per scenario; the baseline CPU run came to
$0.0058 per 1k requests. The full table with real numbers is in
[evals/reports/benchmark.md](../evals/reports/benchmark.md).

Three structural savings are built into the topology:

- **Pod autoscaling** returns model-server to its 2-replica floor when traffic
  drops (the HPA's scale-down stabilization is minutes, not hours).
- **The GPU group sits at min 0 / desired 0**, so GPU spend is zero until a
  GPU workload actually exists.
- **The taint keeps CPU workloads off GPU nodes**, so a stray scheduling
  accident never burns A10G hours on the fraud classifier.

The dedicated analysis (CPU vs GPU $/hr, cost-per-1k per scenario, spot
nodes, autoscaling savings) is in [cost-analysis.md](cost-analysis.md); every
number there traces back to the benchmark report.

---

## See also

- [architecture.md](architecture.md): system topology, request path, model lifecycle, observability.
- [kubernetes-ml-workloads.md](kubernetes-ml-workloads.md): manifest-field rationale, GPU scheduling mechanics.
- [autoscaling-strategy.md](autoscaling-strategy.md): HPA vs KEDA vs Karpenter, node autoscaling caveats.
- [cost-analysis.md](cost-analysis.md): CPU vs GPU economics and measured cost-per-1k.
- [observability.md](observability.md): the metrics/traces/logs stack and SLI/SLO contract.
- [serving/fastapi/deployment.md](../serving/fastapi/deployment.md): kind + EKS deployment guide with the canary flow.
- [infra/terraform/envs/prod/README.md](../infra/terraform/envs/prod/README.md): the env root's own real-AWS vs offline note.
