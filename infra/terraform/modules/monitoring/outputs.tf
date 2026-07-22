output "log_group_names" {
  description = "Map of component name -> CloudWatch log group name."
  value       = { for name, group in aws_cloudwatch_log_group.this : name => group.name }
}

output "log_group_arns" {
  description = "Map of component name -> CloudWatch log group ARN."
  value       = { for name, group in aws_cloudwatch_log_group.this : name => group.arn }
}

output "alarm_arns" {
  description = "Map of alarm key (http_5xx, latency_p95) -> alarm ARN."
  value = {
    http_5xx    = aws_cloudwatch_metric_alarm.http_5xx.arn
    latency_p95 = aws_cloudwatch_metric_alarm.latency_p95.arn
  }
}

output "sns_topic_arn" {
  description = "ARN of the alarms SNS topic, or null when var.alarm_email is empty."
  value       = local.create_sns ? aws_sns_topic.alarms[0].arn : null
}
