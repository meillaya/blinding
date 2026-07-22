output "alb_dns_name" {
  description = "DNS name of the ALB (point a CNAME / the ingress at this)."
  value       = aws_lb.this.dns_name
}

output "target_group_arn" {
  description = "ARN of the model-server target group."
  value       = aws_lb_target_group.this.arn
}

output "alb_arn" {
  description = "ARN of the ALB."
  value       = aws_lb.this.arn
}

output "alb_arn_suffix" {
  description = "ARN suffix of the ALB (the LoadBalancer dimension for CloudWatch AWS/ApplicationELB metrics, used by the monitoring module)."
  value       = aws_lb.this.arn_suffix
}

output "listener_arn" {
  description = "ARN of the HTTP listener."
  value       = aws_lb_listener.http.arn
}

output "security_group_id" {
  description = "ID of the ALB security group."
  value       = aws_security_group.alb.id
}
