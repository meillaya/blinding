# VPC for the InferenceOps EKS platform.
#
# Thin wrapper around the community module terraform-aws-modules/vpc/aws (~> 6.6)
# that pins the settings EKS + the AWS Load Balancer Controller require:
#   * public subnets tagged  kubernetes.io/role/elb=1          (internet-facing LBs)
#   * private subnets tagged kubernetes.io/role/internal-elb=1 (internal LBs)
#   * one NAT gateway per AZ (HA egress for private-subnet workloads: ECR pulls, etc.)
#   * DNS hostnames + DNS support enabled (required for EKS node private DNS)
#
# The outputs (vpc_id, private_subnets, public_subnets) are consumed by the
# EKS module (todo 23) and wired up by the env roots (todo 24).

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.6"

  name = var.name
  cidr = var.cidr

  azs             = var.azs
  public_subnets  = var.public_subnets
  private_subnets = var.private_subnets

  # Required for EKS (node private DNS resolution + cluster DNS add-on).
  enable_dns_hostnames = var.enable_dns_hostnames
  enable_dns_support   = var.enable_dns_support

  # NAT gateway(s) so private-subnet nodes can reach ECR / the internet.
  enable_nat_gateway     = var.enable_nat_gateway
  one_nat_gateway_per_az = var.one_nat_gateway_per_az

  # AWS Load Balancer Controller discovery tags.
  public_subnet_tags  = var.public_subnet_tags
  private_subnet_tags = var.private_subnet_tags

  tags = var.tags
}
