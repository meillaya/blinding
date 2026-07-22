locals {
  # CPU node group is always present.
  cpu_node_group = {
    cpu = {
      name           = "${var.name}-cpu"
      instance_types = ["m6i.xlarge"]
      min_size       = 2
      max_size       = 6
      desired_size   = 2

      # Offline guard (todo 24): set false so moto is not asked for the SSM AMI parameter.
      use_latest_ami_release_version = var.use_latest_ami_release_version
    }
  }

  # GPU node group is optional, gated by var.enable_gpu_node_group.
  gpu_node_group = var.enable_gpu_node_group ? {
    gpu = {
      name           = "${var.name}-gpu"
      instance_types = ["g5.xlarge"]
      ami_type       = "BOTTLEROCKET_x86_64_NVIDIA"
      min_size       = 0
      max_size       = 4
      desired_size   = 0

      use_latest_ami_release_version = var.use_latest_ami_release_version

      taints = {
        gpu = {
          key    = "nvidia.com/gpu"
          value  = "present"
          effect = "NO_SCHEDULE"
        }
      }
    }
  } : {}

  eks_managed_node_groups = merge(local.cpu_node_group, local.gpu_node_group)
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.24"

  # v21 renames: `name` (was cluster_name), `kubernetes_version` (was cluster_version).
  name               = var.name
  kubernetes_version = var.kubernetes_version
  enable_irsa        = var.enable_irsa

  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids

  eks_managed_node_groups = local.eks_managed_node_groups

  # v21 rename: `addons` (was cluster_addons). Pass `{}` to omit (offline plan, todo 24).
  addons = var.addons

  tags = var.tags
}

# Karpenter: the standalone repo is gone; use the EKS submodule. v21 dropped the
# Karpenter IRSA variables in favour of EKS Pod Identity. Gated by var.enable_karpenter.
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 21.24"

  create = var.enable_karpenter

  cluster_name = var.name

  create_pod_identity_association = true

  tags = var.tags
}
