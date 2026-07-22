output "vpc_id" {
  description = "ID of the environment VPC."
  value       = module.vpc.vpc_id
}

output "private_subnets" {
  description = "Private subnet IDs (EKS nodes)."
  value       = module.vpc.private_subnets
}

output "public_subnets" {
  description = "Public subnet IDs (ALB)."
  value       = module.vpc.public_subnets
}

output "ecr_repository_urls" {
  description = "Logical name -> ECR repository URL (gateway, model-server, worker)."
  value       = module.ecr.repository_urls
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API server endpoint."
  value       = module.eks.cluster_endpoint
}

output "oidc_provider" {
  description = "Cluster OIDC issuer (without https://) backing the IRSA role; empty until IRSA is enabled (offline_plan=false)."
  value       = module.eks.oidc_provider
}

output "model_server_irsa_role_arn" {
  description = "ARN of the model-server IRSA role (S3 read access to the artifacts bucket)."
  value       = module.iam.role_arn
}

output "model_artifacts_bucket_id" {
  description = "Name of the model-artifacts bucket."
  value       = module.s3.bucket_id
}

output "model_artifacts_bucket_arn" {
  description = "ARN of the model-artifacts bucket."
  value       = module.s3.bucket_arn
}

output "alb_dns_name" {
  description = "DNS name of the internet-facing ALB."
  value       = module.alb.alb_dns_name
}

output "target_group_arn" {
  description = "ARN of the model-server target group."
  value       = module.alb.target_group_arn
}

output "log_group_names" {
  description = "Component -> CloudWatch log group name."
  value       = module.monitoring.log_group_names
}

output "alarm_arns" {
  description = "http_5xx / latency_p95 -> alarm ARN."
  value       = module.monitoring.alarm_arns
}
