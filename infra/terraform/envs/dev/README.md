# dev environment

Dev root for the InferenceOps AWS platform. Wires all six modules —
[vpc](../../modules/vpc/), [ecr](../../modules/ecr/), [eks](../../modules/eks/),
[iam](../../modules/iam/), [s3](../../modules/s3/), [alb](../../modules/alb/) and
[monitoring](../../modules/monitoring/) — and supports a fully **OFFLINE**
`terraform plan` against a local [moto](https://github.com/getmoto/moto) mock
endpoint (todo 24).

## Offline plan (no AWS credentials)

```bash
# 1. start the mock AWS endpoint (moto_server on http://localhost:5000)
scripts/tf_mock.sh start

# 2. init + plan with no AWS credentials in the environment
cd infra/terraform/envs/dev
terraform init
terraform plan
```

How it works (the Oracle B1 fix):

1. `providers.tf` — when `offline_plan = true` (the default) the AWS provider
   sets `skip_credentials_validation`, `skip_requesting_account_id`,
   `skip_metadata_api_check`, static `mock`/`mock` keys, and a
   `dynamic "endpoints"` block pointing `sts, iam, ec2, eks, s3, elbv2,
   cloudwatch, logs, ssm` at `http://localhost:5000`.
2. `main.tf` — the three EKS guards avoid the data sources moto cannot serve:
   - `use_latest_ami_release_version = false` → no `data "aws_ssm_parameter"`
     (the EKS-optimized AMI lives under the reserved `/aws/` SSM namespace that
     moto rejects writes to — it cannot be seeded);
   - `addons = {}` → no `data "aws_eks_addon_version"`;
   - `enable_irsa = false` → no `data "tls_certificate"`.
3. moto answers the data sources that ARE mockable — `aws_caller_identity` /
   `aws_iam_session_context` (STS) and `aws_subnet` / availability zones (EC2);
   `aws_partition` / `aws_region` resolve locally.

## Real-AWS plan/apply

```bash
terraform plan -var offline_plan=false   # needs real credentials (AWS_PROFILE etc.)
```

`offline_plan = false` removes the endpoints/skips (normal provider) and
restores AMI resolution, the core addons (`coredns`, `kube-proxy`, `vpc-cni`)
and IRSA/OIDC.

## Layout

| File | Purpose |
|------|---------|
| `main.tf` | Wires the six modules + the three EKS offline guards. |
| `providers.tf` | Offline moto-backed provider (`dynamic "endpoints"`). |
| `variables.tf` | `offline_plan` (default `true`), region, sizing knobs. |
| `terraform.tfvars.example` | Copy to `terraform.tfvars` to override. |
| `outputs.tf` | Cluster, bucket, ALB and monitoring outputs. |
