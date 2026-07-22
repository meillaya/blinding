output "vpc_id" {
  description = "ID of the VPC. Consumed by the EKS module (todo 23)."
  value       = module.vpc.vpc_id
}

output "private_subnets" {
  description = "IDs of the private subnets. EKS control-plane ENIs and node groups attach here."
  value       = module.vpc.private_subnets
}

output "public_subnets" {
  description = "IDs of the public subnets. Used for internet-facing load balancers."
  value       = module.vpc.public_subnets
}
