# ECR module

Creates the ECR repositories for the InferenceOps container images — by default
the three the platform ships: **gateway**, **model-server**, **worker**.

Hardened defaults:

- `image_tag_mutability = "IMMUTABLE"` — tags cannot be overwritten, so a deployed
  tag always resolves to exactly one image digest (reproducible deploys).
- `image_scanning_configuration { scan_on_push = true }` — every pushed image is
  scanned for CVEs automatically.

## Usage

```hcl
module "ecr" {
  source = "../../modules/ecr"

  # defaults: ["gateway", "model-server", "worker"]
  name_prefix = "inferenceops-dev-"

  tags = { Environment = "dev" }
}

# module.ecr.repository_urls["gateway"] → "<acct>.dkr.ecr.<region>.amazonaws.com/inferenceops-dev-gateway"
```

## Requirements

| Name      | Version  |
|-----------|----------|
| terraform | >= 1.5.7 |
| aws       | >= 6.52  |

## Inputs

| Name                   | Description                                                                          | Type           | Default                              | Required |
|------------------------|--------------------------------------------------------------------------------------|----------------|--------------------------------------|:--------:|
| `repository_names`     | Logical repository names to create (output maps are keyed by these).                 | `list(string)` | `["gateway", "model-server", "worker"]` |    no    |
| `name_prefix`          | Optional prefix prepended to every repository name (env namespacing).                | `string`       | `""`                                 |    no    |
| `image_tag_mutability` | `IMMUTABLE` (default, reproducible deploys) or `MUTABLE`.                            | `string`       | `"IMMUTABLE"`                        |    no    |
| `scan_on_push`         | Scan images for vulnerabilities on push.                                             | `bool`         | `true`                               |    no    |
| `tags`                 | Tags applied to every repository.                                                    | `map(string)`  | `{}`                                 |    no    |

## Outputs

| Name                | Description                                              |
|---------------------|----------------------------------------------------------|
| `repository_urls`   | Map of logical name → ECR repository URL.                |
| `repository_arns`   | Map of logical name → ECR repository ARN (for IAM).      |

## Provider configuration

This module deliberately declares **no** `provider "aws" {}` block — like any
reusable child module it inherits the provider configuration from the calling
root. The real provider configuration (including the offline moto mock
endpoints used for local `terraform plan`) lives in the environment roots under
`infra/terraform/envs/` (todo 24).

Standalone module-level validation still works without a provider config,
because `terraform validate` only needs the provider *schema* (fetched by
`init`), not credentials or endpoints:

```bash
cd infra/terraform/modules/ecr
terraform init -backend=false
terraform validate   # → "Success! The configuration is valid."
```

`terraform plan`/`apply` are intentionally an env-level gate (todo 24), since a
standalone module inherits its provider from the calling root.
