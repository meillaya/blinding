variable "name" {
  description = "Name applied to the VPC and its derived resources (used as the Name tag)."
  type        = string
}

variable "cidr" {
  description = "IPv4 CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "Availability zones the subnets are spread across (e.g. [\"us-east-1a\", \"us-east-1b\", \"us-east-1c\"]). One public + one private subnet is created per AZ, in order."
  type        = list(string)
}

variable "public_subnets" {
  description = "Public subnet CIDR blocks, one per AZ, in the same order as var.azs."
  type        = list(string)
}

variable "private_subnets" {
  description = "Private subnet CIDR blocks, one per AZ, in the same order as var.azs. EKS node groups run here."
  type        = list(string)
}

variable "public_subnet_tags" {
  description = "Extra tags for the public subnets. Default is the tag the AWS Load Balancer Controller uses to discover subnets for internet-facing load balancers."
  type        = map(string)
  default = {
    "kubernetes.io/role/elb" = 1
  }
}

variable "private_subnet_tags" {
  description = "Extra tags for the private subnets. Default is the tag the AWS Load Balancer Controller uses to discover subnets for internal load balancers."
  type        = map(string)
  default = {
    "kubernetes.io/role/internal-elb" = 1
  }
}

variable "enable_nat_gateway" {
  description = "Provision NAT gateway(s) so private-subnet workloads can reach the internet (ECR image pulls, S3, etc.)."
  type        = bool
  default     = true
}

variable "one_nat_gateway_per_az" {
  description = "Create one NAT gateway per AZ (highly available) instead of a single shared NAT gateway. Only meaningful when enable_nat_gateway = true."
  type        = bool
  default     = true
}

variable "enable_dns_hostnames" {
  description = "Enable DNS hostnames in the VPC. Required for EKS nodes to register with private DNS names."
  type        = bool
  default     = true
}

variable "enable_dns_support" {
  description = "Enable DNS resolution in the VPC. Required for EKS and CoreDNS."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to all taggable resources created by the VPC module."
  type        = map(string)
  default     = {}
}
