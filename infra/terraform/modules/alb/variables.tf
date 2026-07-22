variable "name" {
  description = "Name of the ALB, its target group and security group. Max 32 chars, lowercase alphanumerics and hyphens (ALB naming rules)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]{1,32}$", var.name)) && !can(regex("^-$", var.name))
    error_message = "name must be 1-32 lowercase alphanumerics/hyphens (ALB naming rules); got \"${var.name}\"."
  }
}

variable "vpc_id" {
  description = "VPC the ALB security group is created in and the target group binds to."
  type        = string
}

variable "subnet_ids" {
  description = "Public subnet IDs the ALB is attached to (at least two AZs for an application load balancer)."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "subnet_ids must contain at least two subnets (an ALB requires two AZs)."
  }
}

variable "listener_port" {
  description = "Port the HTTP listener accepts client traffic on."
  type        = number
  default     = 80
}

variable "target_port" {
  description = "Port the model-server pods listen on (target group port)."
  type        = number
  default     = 8080
}

variable "health_check_path" {
  description = "HTTP path the target group health check probes."
  type        = string
  default     = "/healthz"
}

variable "ingress_cidr_blocks" {
  description = "CIDR blocks allowed to reach the listener. Default is the whole internet (it is an internet-facing inference endpoint)."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "tags" {
  description = "Tags applied to every resource the module creates."
  type        = map(string)
  default     = {}
}
