# Security

How InferenceOps protects the three things an inference platform actually owns:
the model artifacts, the credentials that reach them, and the request path that
serves them. Every control below is implemented in this repo. The last section
names the controls that are deliberately absent, so the boundary is honest.

The threat model is modest on purpose: a single-tenant fraud-detection API behind
an API key, workloads in private subnets, and model weights that only the
model-server ever reads. Multi-tenant isolation, WAF rules, and mTLS between
services are out of scope (see [section 8](#8-what-is-deliberately-not-hardened)).

## 1. IAM and IRSA: least-privilege pod credentials

On EKS the model-server reads artifacts from S3 through **IAM Roles for Service
Accounts (IRSA)**. No static AWS key ever enters the cluster.

The [`iam` module](../infra/terraform/modules/iam/) wraps
`terraform-aws-modules/iam/aws//modules/iam-role` (`~> 6.6`) with
`enable_oidc = true` and creates exactly one role,
`${name}-model-server-irsa`:

- **Trust is scoped to one service account.** `oidc_subjects =
  ["system:serviceaccount:inferenceops:model-server"]`, so only pods running as
  the `model-server` service account in the `inferenceops` namespace can assume
  the role. The gateway and worker get nothing.
- **The only attached policy is `AmazonS3ReadOnlyAccess`.** The role can read
  objects and list buckets, and cannot write, delete, or touch any other AWS
  service. Least privilege by managed-policy, audited in
  [`infra/terraform/envs/prod/main.tf`](../infra/terraform/envs/prod/main.tf).
- **The binding completes at deploy time.** The base
  [`k8s/base/serviceaccount.yaml`](../k8s/base/serviceaccount.yaml) carries no
  `eks.amazonaws.com/role-arn` annotation (kind has no OIDC provider). On EKS
  you annotate the service account with the module's `role_arn` output; the
  full flow is in [`aws-deployment.md` section 5](./aws-deployment.md#5-irsa-how-the-model-server-pod-gets-s3-access).

The same code runs locally with no cloud credentials at all: the
`ModelStorage` interface has a `LocalStorageBackend` for `models/artifacts` and
an `S3StorageBackend` for EKS, so the credential story never leaks into the
application logic.

IRSA is gated by `enable_irsa`, which the offline plan turns off (guard 3 in
[`aws-deployment.md` section 3](./aws-deployment.md#3-offline-terraform-plan-no-credentials))
because creating the OIDC provider needs the live cluster issuer certificate.
The real-AWS path (`offline_plan=false`) turns it on.

## 2. No secrets in the repo

- **`.env` is git-ignored** ([`.gitignore`](../.gitignore), line 2), along with
  Terraform state (`*.tfstate*`) and binary model weights
  (`models/artifacts/**/*.pkl`). State files can contain credentials, so they
  never land in git either.
- **The only committed secret manifest is an example.**
  [`k8s/base/secret.example.yaml`](../k8s/base/secret.example.yaml) creates the
  `gateway-api-keys` Secret with two obviously-fake dev keys
  (`dev-key-REPLACE-ME-0000000000000000`,
  `dev-key-REPLACE-ME-1111111111111111`). The file's header says to copy it to a
  real `secret.yaml` and replace the values; real keys are applied out-of-band
  and never committed. The dev keys exist so `make deploy-local` works
  out-of-the-box on a laptop, and are safe to print in docs because they are
  only ever valid against a local kind cluster.
- **CI authenticates with OIDC, not keys.**
  [`.github/workflows/build-and-push.yml`](../.github/workflows/build-and-push.yml)
  sets `permissions: id-token: write` and uses
  `aws-actions/configure-aws-credentials` with `role-to-assume:
  ${{ secrets.AWS_ROLE_ARN }}`. Every credential and registry value comes from
  `${{ secrets.* }}`; nothing is inlined. The ECR push is double-gated on the
  `main` branch and the presence of the required secrets, so a fork or a PR
  builds and scans but cannot publish.

## 3. Non-root, pinned, multi-stage containers

Both shipped images follow the same shape:

- **Multi-stage builds.** A builder stage materializes the uv workspace venv;
  the runtime stage copies only the venv, the package source, and (for the
  model-server) the trained artifact. Build tooling never ships in the runtime
  image. See [`apps/model-server/Dockerfile`](../apps/model-server/Dockerfile)
  and [`apps/gateway/Dockerfile`](../apps/gateway/Dockerfile).
- **Non-root runtime.** Each Dockerfile creates `appuser` (uid 1000) and drops
  to `USER appuser` before `CMD`. The model-server only reads the model and
  writes nothing persistent, so read-only access to `/app` suffices.
- **Pinned bases and tools.** Runtime is `python:3.12-slim` (a pinned minor,
  not `latest`), and uv is copied from `ghcr.io/astral-sh/uv:0.11.30`. hadolint
  passes with zero inline ignores, which is what pinning buys you.
- **A stdlib healthcheck.** `python:3.12-slim` ships no curl, so the
  HEALTHCHECK probes `/healthz` with `urllib.request` instead of adding a
  package just to probe.

## 4. Image supply chain: ECR scanning and immutable tags

The [`ecr` module](../infra/terraform/modules/ecr/) creates the three
repositories the platform ships (gateway, model-server, worker) with two
hardened defaults:

- **`image_scanning_configuration { scan_on_push = true }`.** Every push
  triggers a CVE scan automatically; findings land in ECR without a separate
  scanner to operate.
- **`image_tag_mutability = "IMMUTABLE"`.** A tag cannot be overwritten, so a
  deployed tag always resolves to exactly one image digest. Reproducible
  deploys and no silent image swaps.

CI adds two more gates in
[`build-and-push.yml`](../.github/workflows/build-and-push.yml): **Trivy v0.56.2
fails the build on HIGH/CRITICAL CVEs** (`trivy image --severity HIGH,CRITICAL
--exit-code 1`) before anything is pushed, and the push itself only runs on
`main` with OIDC credentials present.

## 5. Network isolation: private subnets, NAT, security groups

Network segmentation lives at the AWS layer, not in Kubernetes NetworkPolicy
objects (see [section 8](#8-what-is-deliberately-not-hardened)):

- **Workloads run in private subnets.** The EKS module receives
  `subnet_ids = module.vpc.private_subnets`
  ([`envs/prod/main.tf`](../infra/terraform/envs/prod/main.tf)), so the
  control-plane ENIs and every node (the `m6i` CPU group and the optional `g5`
  GPU group) sit in private subnets with no direct internet route.
- **Egress goes through NAT, per AZ.** The
  [`vpc` module](../infra/terraform/modules/vpc/) enables
  `one_nat_gateway_per_az`, giving private-subnet nodes highly-available egress
  for ECR pulls, S3 reads, and the OIDC handshake, while inbound initiation
  from the internet stays impossible.
- **One public entry point.** The [`alb` module](../infra/terraform/modules/alb/)
  puts the Application Load Balancer in the public subnets (tagged
  `kubernetes.io/role/elb=1`). Its security group opens only the listener port;
  `drop_invalid_header_fields = true` sheds request-smuggling-shaped traffic at
  the edge. The private subnets carry the
  `kubernetes.io/role/internal-elb=1` tag, so the AWS Load Balancer Controller
  can also create internal load balancers when a service should never face the
  internet.
- **Pod-to-pod traffic stays in-cluster.** The request path is
  ingress-nginx → gateway → model-server over ClusterIP Services; only the
  ingress (kind) or the ALB (EKS) is exposed.

## 6. API-key auth and rate limiting at the edge

The gateway is the trust boundary. Every request except `/healthz` passes
through `AuthMiddleware`
([`apps/gateway/src/inferenceops_gateway/auth.py`](../apps/gateway/src/inferenceops_gateway/auth.py)):

- **`X-API-Key` is mandatory.** A missing or unknown key gets a 401 with a plain
  JSON detail. `authenticate` is the pure core; the middleware is the ASGI edge.
- **Fail-closed on misconfiguration.** An empty configured key set rejects
  every request rather than open-authenticating. A broken deploy fails shut.
- **Probes need no credentials.** `/healthz` is exempt so Kubernetes liveness
  and readiness probes work without handing keys to the kubelet.
- **Per-key rate limiting.** Behind auth, a token-bucket limiter
  ([`rate_limit.py`](../apps/gateway/src/inferenceops_gateway/rate_limit.py))
  keys one bucket per API key and sheds excess load with 429 + `Retry-After`,
  so one noisy tenant cannot starve the others.

The authenticated principal is published into the ASGI scope, which is how the
rate limiter knows which bucket to debit. Auth and limiting share one identity
seam instead of re-parsing the header.

## 7. Model artifact storage

The [`s3` module](../infra/terraform/modules/s3/) hardens the artifacts bucket:

- **All public access blocked** (`block_public_acls`, `block_public_policy`,
  `ignore_public_acls`, `restrict_public_buckets`). Weights are never served
  from S3 directly; the model-server fetches them over IRSA-scoped reads.
- **Versioning enabled.** Every artifact write is immutable-by-history, so a
  bad upload rolls back to the previous object version. This complements the
  application-level upload ordering (bytes first, metadata last) documented in
  [`interview-deep-dives.md`](./interview-deep-dives.md): a partial upload
  never publishes a model, and S3 versioning means even a completed bad upload
  is recoverable.
- **SSE-AES256 at rest** with S3-managed keys. Encryption without a KMS
  dependency, so the bucket works in every account and region, including the
  offline moto endpoint used for CI plans.
- **`force_destroy` off in prod.** A non-empty prod bucket cannot be destroyed
  by a stray `terraform destroy`.

## 8. What is deliberately NOT hardened

Honest gaps, each a conscious scope decision rather than an oversight:

- **No Kubernetes NetworkPolicy objects.** Isolation is enforced at the AWS
  layer (private subnets, security groups, the internal-elb tag). Adding
  in-cluster NetworkPolicies is a natural next step on EKS, where the CNI
  enforces them; kind's default kindnet does not.
- **The ALB listener is HTTP, not HTTPS.** The module ships an HTTP listener so
  the offline plan and the local demo need no certificate plumbing. On real AWS
  you terminate TLS at the ALB (or at ingress-nginx behind it, which the kind
  path already exercises with cert-manager installed for KServe).
- **Single-tenant API keys.** Keys map to one `default` tenant. There is no
  per-tenant authorization beyond the rate-limit bucket, which matches the
  stated scope: no multi-tenant auth beyond simple API-key.
- **Dev key values are committed (as examples).** Safe because they only work
  against a local kind cluster, and the deploy script overwrites them with a
  different local key anyway. They would be unacceptable in any shared
  environment, which is exactly what the REPLACE-ME naming shouts.

## See also

- [`docs/aws-deployment.md`](./aws-deployment.md): the full VPC, EKS, IRSA, and ALB walkthrough.
- [`docs/observability.md`](./observability.md): who can see what, and the audit trail (traces, logs, metrics).
- [`docs/incident-runbook.md`](./incident-runbook.md): what happens when a control fails in production.
- [`docs/interview-deep-dives.md`](./interview-deep-dives.md): IRSA and storage identity as interview Q&A.
