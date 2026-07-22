output "repository_urls" {
  description = "Map of logical repository name -> ECR repository URL (e.g. { \"gateway\" = \"123456789012.dkr.ecr.us-east-1.amazonaws.com/inferenceops-dev-gateway\" })."
  value       = { for name, repo in aws_ecr_repository.this : name => repo.repository_url }
}

output "repository_arns" {
  description = "Map of logical repository name -> ECR repository ARN (for IAM policies granting push/pull)."
  value       = { for name, repo in aws_ecr_repository.this : name => repo.arn }
}
