# InferenceOps — staging environment root.
#
# Wires the six platform modules (vpc, ecr, eks, iam, s3, alb, monitoring) and
# carries the OFFLINE-plan machinery (todo 24): the three EKS guards below plus
# the moto-backed provider in providers.tf make `terraform plan` succeed with no
# AWS credentials against scripts/tf_mock.sh (moto_server on localhost:5000).

locals {
  # AWS caps IAM role name_prefix at 38 chars and the EKS module derives the
  # node-group role prefix as "<name>-<group>-eks-node-group-"; abbreviate long
  # environment names so that prefix stays within the limit (staging -> stg).
  name_abbrev = { dev = "dev", staging = "stg", prod = "prod" }[var.environment]
  name        = "inferenceops-${local.name_abbrev}"

  tags = merge(
    {
      Project     = "InferenceOps"
      Environment = var.environment
      ManagedBy   = "terraform"
    },
    var.tags,
  )
}

module "vpc" {
  source = "../../modules/vpc"

  name = local.name
  cidr = var.vpc_cidr

  azs             = var.azs
  public_subnets  = var.public_subnets
  private_subnets = var.private_subnets

  tags = local.tags
}

module "ecr" {
  source = "../../modules/ecr"

  name_prefix = "${local.name}-"

  tags = local.tags
}

module "eks" {
  source = "../../modules/eks"

  name               = local.name
  kubernetes_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  enable_gpu_node_group = var.enable_gpu_node_group
  enable_karpenter      = var.enable_karpenter

  # ── The three EKS OFFLINE guards (todo 24) ────────────────────────────────
  # skip_* + mock keys suppress only the provider's OWN init calls; data sources
  # are still read at plan time. These guards AVOID the data sources moto cannot
  # serve, so the offline plan genuinely succeeds:
  #   1. use_latest_ami_release_version=false → no data "aws_ssm_parameter"
  #      (the EKS-optimized AMI lives under the reserved /aws/ SSM namespace,
  #      which moto rejects PutParameter for — it cannot be seeded).
  #   2. addons={} → no data "aws_eks_addon_version" (read for EVERY configured
  #      addon; moto 404s DescribeAddonVersions).
  #   3. enable_irsa=false → no data "tls_certificate" (live HTTPS fetch of the
  #      cluster OIDC issuer cert — impossible before the cluster exists).
  # The real-AWS path (offline_plan=false) restores AMI resolution, the core
  # addons and IRSA/OIDC.
  use_latest_ami_release_version = var.offline_plan ? false : true
  addons                         = var.offline_plan ? {} : { coredns = {}, kube-proxy = {}, vpc-cni = {} }
  enable_irsa                    = var.offline_plan ? false : true

  tags = local.tags
}

module "iam" {
  source = "../../modules/iam"

  role_name         = "${local.name}-model-server-irsa"
  oidc_provider_url = module.eks.oidc_provider
  oidc_subjects     = ["system:serviceaccount:inferenceops:model-server"]
  policies = {
    S3ReadOnly = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
  }

  tags = local.tags
}

module "s3" {
  source = "../../modules/s3"

  bucket_name   = "${local.name}-model-artifacts"
  force_destroy = var.environment != "prod"

  tags = local.tags
}

module "alb" {
  source = "../../modules/alb"

  name       = local.name
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  listener_port = var.alb_listener_port
  target_port   = var.model_server_port

  tags = local.tags
}

module "monitoring" {
  source = "../../modules/monitoring"

  name           = local.name
  alarm_email    = var.alarm_email
  alb_arn_suffix = module.alb.alb_arn_suffix

  tags = local.tags
}
