variable "role_name" {
  description = "Exact name of the IAM role to create (use_name_prefix is disabled so the name is used verbatim)"
  type        = string
}

variable "oidc_provider_url" {
  description = "OIDC provider issuer URL WITHOUT the leading https:// (e.g. module.eks.oidc_provider)"
  type        = string
}

variable "oidc_subjects" {
  description = "Fully qualified OIDC subjects (`system:serviceaccount:<namespace>:<serviceaccount>`) trusted by the role"
  type        = list(string)
  default     = ["system:serviceaccount:inferenceops:model-server"]
}

variable "policies" {
  description = "Policies to attach to the role in `{ Name = \"arn:aws:iam::aws:policy/...\" }` format"
  type        = map(string)
  default = {
    S3ReadOnly = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
  }
}

variable "tags" {
  description = "A map of tags to add to all resources"
  type        = map(string)
  default     = {}
}
