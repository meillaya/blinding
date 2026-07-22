output "bucket_arn" {
  description = "ARN of the model-artifacts bucket (for IAM policies scoping model-server reads)."
  value       = module.s3_bucket.s3_bucket_arn
}

output "bucket_id" {
  description = "Name (ID) of the model-artifacts bucket."
  value       = module.s3_bucket.s3_bucket_id
}
