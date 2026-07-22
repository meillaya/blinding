variable "name" {
  description = "Name of the EKS cluster (v21: was `cluster_name`)"
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes `<major>.<minor>` version for the EKS cluster (v21: was `cluster_version`). Pinned to 1.33."
  type        = string
  default     = "1.33"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+$", var.kubernetes_version))
    error_message = "kubernetes_version must be a <major>.<minor> string such as \"1.33\"; got \"${var.kubernetes_version}\"."
  }
}

variable "vpc_id" {
  description = "ID of the VPC where the cluster security group will be provisioned"
  type        = string
}

variable "subnet_ids" {
  description = "A list of subnet IDs where the nodes/node groups will be provisioned"
  type        = list(string)
}

variable "enable_gpu_node_group" {
  description = "Whether to create the optional GPU (g5.xlarge) managed node group, tainted nvidia.com/gpu=present:NoSchedule"
  type        = bool
  default     = false
}

variable "enable_karpenter" {
  description = "Whether to provision the Karpenter submodule (pod-identity association; v21 dropped the Karpenter IRSA variables)"
  type        = bool
  default     = false
}

variable "enable_irsa" {
  description = "Whether to create the OIDC provider for IRSA. Gate this OFFLINE (todo 24) because the OIDC tls_certificate data source needs a reachable cluster issuer."
  type        = bool
  default     = true
}

variable "use_latest_ami_release_version" {
  description = "Whether each managed node group resolves the latest EKS-optimized AMI via the SSM data source. Set FALSE offline (todo 24) so moto is not asked for the reserved /aws/ SSM parameter."
  type        = bool
  default     = true
}

variable "addons" {
  description = "Map of cluster addon configurations (v21: was `cluster_addons`). Pass `{}` to omit addons (offline plan, todo 24)."
  type        = map(any)
  default     = {}
}

variable "tags" {
  description = "A map of tags to add to all resources"
  type        = map(string)
  default     = {}
}
