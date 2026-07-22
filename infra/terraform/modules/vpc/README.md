# VPC module

Thin wrapper around [`terraform-aws-modules/vpc/aws` `~> 6.6`](https://registry.terraform.io/modules/terraform-aws-modules/vpc/aws)
pinned to the settings the InferenceOps EKS platform needs:

- public subnets tagged `kubernetes.io/role/elb = 1` (AWS Load Balancer Controller → internet-facing LBs)
- private subnets tagged `kubernetes.io/role/internal-elb = 1` (AWS Load Balancer Controller → internal LBs)
- `enable_nat_gateway = true` + `one_nat_gateway_per_az = true` (HA egress for private-subnet nodes: ECR pulls, S3, etc.)
- DNS hostnames + DNS support enabled (required for EKS node private DNS / CoreDNS)

Its outputs (`vpc_id`, `private_subnets`, `public_subnets`) are consumed by the EKS
module (todo 23) and wired up by the environment roots in `infra/terraform/envs/` (todo 24).

## Usage

```hcl
module "vpc" {
  source = "../../modules/vpc"

  name = "inferenceops-dev"
  cidr = "10.0.0.0/16"

  azs             = ["us-east-1a", "us-east-1b", "us-east-1c"]
  public_subnets  = ["10.0.0.0/20", "10.0.16.0/20", "10.0.32.0/20"]
  private_subnets = ["10.0.48.0/20", "10.0.64.0/20", "10.0.80.0/20"]

  tags = { Environment = "dev" }
}
```

## Requirements

| Name        | Version   |
|-------------|-----------|
| terraform   | >= 1.5.7  |
| aws         | >= 6.52   |
| vpc module  | ~> 6.6    |

## Inputs

| Name                     | Description                                                            | Type           | Default                                          | Required |
|--------------------------|------------------------------------------------------------------------|----------------|--------------------------------------------------|:--------:|
| `name`                   | Name applied to the VPC and derived resources (Name tag).              | `string`       | n/a                                              |   yes    |
| `cidr`                   | IPv4 CIDR block for the VPC.                                           | `string`       | `"10.0.0.0/16"`                                  |    no    |
| `azs`                    | Availability zones, one public + one private subnet per AZ, in order.  | `list(string)` | n/a                                              |   yes    |
| `public_subnets`         | Public subnet CIDRs, one per AZ (same order as `azs`).                 | `list(string)` | n/a                                              |   yes    |
| `private_subnets`        | Private subnet CIDRs, one per AZ (same order as `azs`).                | `list(string)` | n/a                                              |   yes    |
| `public_subnet_tags`     | Extra tags for public subnets (LB Controller discovery).               | `map(string)`  | `{ "kubernetes.io/role/elb" = 1 }`               |    no    |
| `private_subnet_tags`    | Extra tags for private subnets (LB Controller discovery).              | `map(string)`  | `{ "kubernetes.io/role/internal-elb" = 1 }`      |    no    |
| `enable_nat_gateway`     | Provision NAT gateway(s) for private-subnet egress.                    | `bool`         | `true`                                           |    no    |
| `one_nat_gateway_per_az` | One NAT gateway per AZ (HA) instead of a single shared one.            | `bool`         | `true`                                           |    no    |
| `enable_dns_hostnames`   | VPC DNS hostnames (required for EKS node private DNS).                 | `bool`         | `true`                                           |    no    |
| `enable_dns_support`     | VPC DNS resolution (required for EKS / CoreDNS).                       | `bool`         | `true`                                           |    no    |
| `tags`                   | Tags applied to all taggable VPC resources.                            | `map(string)`  | `{}`                                             |    no    |

## Outputs

| Name              | Description                                                    |
|-------------------|----------------------------------------------------------------|
| `vpc_id`          | ID of the VPC (consumed by the EKS module).                    |
| `private_subnets` | IDs of the private subnets (EKS node groups / control-plane).  |
| `public_subnets`  | IDs of the public subnets (internet-facing load balancers).    |

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
cd infra/terraform/modules/vpc
terraform init -backend=false
terraform validate   # → "Success! The configuration is valid."
```

`terraform plan`/`apply` are intentionally an env-level gate (todo 24), since a
standalone module inherits its provider from the calling root.
