# IAM IRSA Module

Wraps [`terraform-aws-modules/iam/aws//modules/iam-role`](https://registry.terraform.io/modules/terraform-aws-modules/iam/aws/latest) `~> 6.6` to create an IAM role trusted by the EKS OIDC provider for **IRSA** (IAM Roles for Service Accounts).

## Why `iam-role` and not `iam-assumable-role-with-oidc`

In `terraform-aws-iam` **v6** the `iam-assumable-role-with-oidc` submodule was **removed** and merged
into `iam-role`. Verified against the pinned `v6.6.0` tag:

- `modules/iam-assumable-role-with-oidc/variables.tf` → **HTTP 404** (submodule no longer ships).
- `modules/iam-role/variables.tf` (v6.6.0) defines the OIDC inputs used here:
  `enable_oidc` (bool, default `false`), `oidc_provider_urls` (list(string)),
  `oidc_subjects` (list(string)), and `policies` (map(string), `{ Name = "policy_arn" }`).

So this module uses `iam-role` with `enable_oidc = true`. The role trusts the cluster OIDC provider
for the `model-server` service account by default
(`oidc_subjects = ["system:serviceaccount:inferenceops:model-server"]`) and attaches
`AmazonS3ReadOnlyAccess`.

`use_name_prefix = false` so `role_name` is used verbatim (the module default treats it as a prefix).

## Usage

```hcl
module "iam" {
  source = "../modules/iam"

  role_name         = "inferenceops-model-server"
  oidc_provider_url = module.eks.oidc_provider # issuer URL without https://
}
```

## Inputs

| Name                | Description                                                              | Default                                              |
| ------------------- | ------------------------------------------------------------------------ | ---------------------------------------------------- |
| `role_name`         | Exact IAM role name                                                       | (required)                                           |
| `oidc_provider_url` | OIDC issuer URL **without** `https://` (`module.eks.oidc_provider`)       | (required)                                           |
| `oidc_subjects`     | Fully qualified OIDC subjects trusted by the role                         | `["system:serviceaccount:inferenceops:model-server"]`|
| `policies`          | `{ Name = "policy_arn" }` map of policies to attach                       | `{ S3ReadOnly = ".../AmazonS3ReadOnlyAccess" }`      |
| `tags`              | Tags for all resources                                                    | `{}`                                                 |

## Outputs

| Name        | Description            |
| ----------- | ---------------------- |
| `role_arn`  | The ARN of the IAM role|
| `role_name` | The name of the IAM role|

The trust relationship (role ↔ OIDC provider for the `model-server` SA) is verified at the env level in todo 24.
