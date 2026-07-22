output "role_arn" {
  description = "The ARN of the IAM role"
  value       = module.iam_role.arn
}

output "role_name" {
  description = "The name of the IAM role"
  value       = module.iam_role.name
}
