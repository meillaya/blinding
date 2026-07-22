variable "bucket_name" {
  description = "Globally unique name of the model-artifacts bucket (e.g. \"inferenceops-dev-model-artifacts\"). Lowercase letters, numbers and hyphens only."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be a valid S3 bucket name (lowercase letters, numbers, dots and hyphens, 3-63 chars); got \"${var.bucket_name}\"."
  }
}

variable "force_destroy" {
  description = "Allow destroying the bucket even when it still contains objects. Convenient for dev/staging teardown; keep false for prod."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to the bucket."
  type        = map(string)
  default     = {}
}
