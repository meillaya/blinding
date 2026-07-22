variable "environment" {
  description = "Environment name; prefixes every resource (inferenceops-<environment>; staging is abbreviated to stg — see main.tf — to stay within the IAM 38-char name_prefix limit)."
  type        = string
  default     = "staging"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "offline_plan" {
  description = "OFFLINE mock mode (todo 24). true = point the AWS provider at the local moto server (scripts/tf_mock.sh) with mock credentials and activate the three EKS guards, so `terraform plan` succeeds with no AWS credentials. false = real AWS (needs credentials; creates OIDC/IRSA, resolves real AMIs + addon versions)."
  type        = bool
  default     = true
}

variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes <major>.<minor> version."
  type        = string
  default     = "1.33"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "Availability zones (one public + one private subnet per AZ)."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

variable "public_subnets" {
  description = "Public subnet CIDRs, one per AZ (ALB lives here)."
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
}

variable "private_subnets" {
  description = "Private subnet CIDRs, one per AZ (EKS nodes live here)."
  type        = list(string)
  default     = ["10.0.11.0/24", "10.0.12.0/24", "10.0.13.0/24"]
}

variable "enable_gpu_node_group" {
  description = "Create the optional GPU (g5.xlarge) managed node group, tainted nvidia.com/gpu=present:NoSchedule."
  type        = bool
  default     = false
}

variable "enable_karpenter" {
  description = "Provision the Karpenter submodule (pod-identity association)."
  type        = bool
  default     = false
}

variable "alb_listener_port" {
  description = "Port the internet-facing ALB listener accepts client traffic on."
  type        = number
  default     = 80
}

variable "model_server_port" {
  description = "Port the model-server pods listen on (ALB target-group port)."
  type        = number
  default     = 8080
}

variable "alarm_email" {
  description = "Email subscribed to the monitoring alarms SNS topic. \"\" creates the alarms without notification."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Extra tags merged into every resource."
  type        = map(string)
  default     = {}
}
