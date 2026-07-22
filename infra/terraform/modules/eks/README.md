# EKS Module

Wraps [`terraform-aws-modules/eks/aws`](https://registry.terraform.io/modules/terraform-aws-modules/eks/aws/latest) `~> 21.24` to provision an EKS cluster with a CPU managed node group, an optional GPU node group, and an optional Karpenter submodule.

## Module v21 variable renames used here

This module targets EKS module **v21**, which renamed several inputs. We use the **new** names:

| v21 name (used here)   | old name (do NOT use)  |
| ---------------------- | ---------------------- |
| `name`                 | `cluster_name`         |
| `kubernetes_version`   | `cluster_version`      |
| `addons`               | `cluster_addons`       |

`kubernetes_version` is pinned to `"1.33"` by default and validated to be a `<major>.<minor>` string.

## Node groups

- **CPU** (always created): `m6i.xlarge`, min 2 / desired 2 / max 6.
- **GPU** (optional, gated by `enable_gpu_node_group`, default `false`): `g5.xlarge`,
  `ami_type = "BOTTLEROCKET_x86_64_NVIDIA"`, min 0 / desired 0 / max 4, with taint
  `nvidia.com/gpu=present:NO_SCHEDULE` so only GPU-tolerant workloads schedule there.

## Karpenter

Gated by `enable_karpenter` (default `false`). Uses `terraform-aws-modules/eks/aws//modules/karpenter`
with `create_pod_identity_association = true`. The standalone Karpenter repo is gone and v21 dropped
the Karpenter IRSA variables in favour of EKS Pod Identity.

## Offline guards (wired by the env in todo 24)

These let `terraform plan` succeed offline against the moto mock endpoint:

- `use_latest_ami_release_version` (default `true`): set **`false`** offline so the module does not
  read the `aws_ssm_parameter` EKS-optimized-AMI data source (moto rejects the reserved `/aws/…` namespace).
- `addons` (default `{}`): leave **empty** offline — `data "aws_eks_addon_version"` is read for every
  configured addon and moto returns 404 for `DescribeAddonVersions`.
- `enable_irsa` (default `true`): set **`false`** offline — the OIDC `tls_certificate` data source needs a
  reachable cluster issuer URL.

## Usage

```hcl
module "eks" {
  source = "../modules/eks"

  name       = "inferenceops-dev"
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  enable_gpu_node_group = false
  enable_karpenter      = false
}
```

## Outputs

| Name                             | Description                                            |
| -------------------------------- | ------------------------------------------------------ |
| `cluster_name`                   | The name of the EKS cluster                            |
| `cluster_endpoint`               | Endpoint for the EKS cluster API server                |
| `oidc_provider`                  | OIDC issuer URL **without** the leading `https://`     |
| `oidc_provider_arn`              | ARN of the IAM OIDC provider for the cluster           |
| `cluster_certificate_authority_data` | Base64 encoded cluster CA data                     |

`oidc_provider` feeds the IAM IRSA module (`modules/iam`) as `oidc_provider_url`.
