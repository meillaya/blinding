variable "repository_names" {
  description = "Logical names of the ECR repositories to create. Defaults to the three InferenceOps images: gateway, model-server, worker. Output maps are keyed by these logical names."
  type        = list(string)
  default     = ["gateway", "model-server", "worker"]

  validation {
    condition     = length(var.repository_names) > 0
    error_message = "repository_names must contain at least one name."
  }

  validation {
    condition     = length(var.repository_names) == length(distinct(var.repository_names))
    error_message = "repository_names must not contain duplicates."
  }
}

variable "name_prefix" {
  description = "Optional prefix prepended to every repository name (e.g. \"inferenceops-dev-\"). The logical names in var.repository_names are kept as the output map keys."
  type        = string
  default     = ""
}

variable "image_tag_mutability" {
  description = "Image tag mutability. IMMUTABLE (default) forbids tag overwrites so a deployed tag always maps to one image digest (reproducible deploys)."
  type        = string
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["IMMUTABLE", "MUTABLE"], var.image_tag_mutability)
    error_message = "image_tag_mutability must be either \"IMMUTABLE\" or \"MUTABLE\"."
  }
}

variable "scan_on_push" {
  description = "Automatically scan images for vulnerabilities when pushed."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every ECR repository."
  type        = map(string)
  default     = {}
}
