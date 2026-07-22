# alb module

Internet-facing Application Load Balancer fronting the InferenceOps inference
tier: `aws_lb` + `aws_lb_target_group` (IP targets — the model-server pods) +
`aws_lb_listener`, plus a security group that opens the listener port.

Implemented as plain resources (no registry wrapper) so the module contains
**zero data sources** — it plans identically against real AWS and the offline
moto endpoint (todo 24).

## Usage

```hcl
module "alb" {
  source = "../../modules/alb"

  name       = "inferenceops-dev"
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  listener_port = 80
  target_port   = 8080
}
```

## Variables

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `name` | `string` | — | ALB / target-group / SG name (≤32 chars, ALB naming rules). |
| `vpc_id` | `string` | — | VPC for the SG and target group. |
| `subnet_ids` | `list(string)` | — | Public subnets (≥2 AZs). |
| `listener_port` | `number` | `80` | HTTP listener port. |
| `target_port` | `number` | `8080` | model-server pod port. |
| `health_check_path` | `string` | `"/healthz"` | Target-group health-check path. |
| `ingress_cidr_blocks` | `list(string)` | `["0.0.0.0/0"]` | CIDRs allowed to the listener. |
| `tags` | `map(string)` | `{}` | Tags for all resources. |

## Outputs

| Name | Description |
|------|-------------|
| `alb_dns_name` | ALB DNS name. |
| `target_group_arn` | Target group ARN. |
| `alb_arn` | ALB ARN. |
| `alb_arn_suffix` | ALB ARN suffix — the `LoadBalancer` dimension for CloudWatch `AWS/ApplicationELB` metrics (fed to the monitoring module). |
| `listener_arn` | HTTP listener ARN. |
| `security_group_id` | ALB security group ID. |
