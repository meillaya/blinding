# prod environment

Prod root for the InferenceOps AWS platform. Same six-module wiring as
[dev](../dev/) — [vpc](../../modules/vpc/), [ecr](../../modules/ecr/),
[eks](../../modules/eks/), [iam](../../modules/iam/), [s3](../../modules/s3/),
[alb](../../modules/alb/) and [monitoring](../../modules/monitoring/) — with
prod defaults (`environment = "prod"`).

## Real-AWS plan/apply (the prod path)

```bash
# needs real credentials (AWS_PROFILE / env keys); enables IRSA/OIDC, real AMI
# resolution and the core EKS addons (coredns, kube-proxy, vpc-cni)
terraform plan -var offline_plan=false
```

`terraform.tfvars.example` ships with `offline_plan = false` and
`enable_gpu_node_group = true` to document the intended prod shape. Running
`offline_plan=false` with NO credentials fails auth — that failure is the guard
proving this is the real-AWS code path (verified in todo 24 QA).

## Offline plan (CI / local rehearsal, no credentials)

```bash
scripts/tf_mock.sh start          # moto_server on http://localhost:5000
cd infra/terraform/envs/prod
terraform init
terraform plan                    # offline_plan=true (default) + 3 EKS guards
```

## Notable defaults

- `force_destroy` on the artifacts bucket is OFF for prod (non-empty buckets
  cannot be destroyed accidentally).
- GPU node group off by default; prod tfvars enable it.
