# staging environment

Staging root for the InferenceOps AWS platform. Identical wiring to
[dev](../dev/) — [vpc](../../modules/vpc/), [ecr](../../modules/ecr/),
[eks](../../modules/eks/), [iam](../../modules/iam/), [s3](../../modules/s3/),
[alb](../../modules/alb/) and [monitoring](../../modules/monitoring/) — with
staging defaults (`environment = "staging"`). See the dev README for the full
offline-plan mechanics.

## Offline plan (no AWS credentials)

```bash
scripts/tf_mock.sh start          # moto_server on http://localhost:5000
cd infra/terraform/envs/staging
terraform init
terraform plan                    # offline_plan=true (default) + 3 EKS guards
```

## Real-AWS plan/apply

```bash
terraform plan -var offline_plan=false   # needs real credentials
```

## Notable defaults

- `offline_plan = true` — offline moto-backed plan by default.
- `enable_gpu_node_group = false` — enable via tfvars to rehearse GPU scheduling.
- `force_destroy` on the artifacts bucket is on (non-prod convenience).
