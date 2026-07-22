# Observability baseline for an InferenceOps environment.
#
# Plain resources (no data sources) so the module plans identically against real
# AWS and the offline moto endpoint (todo 24):
#   * one CloudWatch log group per platform component (gateway / model-server /
#     worker) — the EKS fluent-bit sidecars ship container logs here;
#   * an optional SNS topic (+ email subscription) that alarms notify;
#   * two SLO alarms on the ALB the alb module creates:
#       - 5xx error count  (HTTPCode_ELB_5XX_Count, Sum)
#       - p95 latency      (TargetResponseTime, extended statistic p95)
#     Both use treat_missing_data = "notBreaching" so a fresh environment with no
#     traffic does not page anyone.

locals {
  create_sns    = var.alarm_email != ""
  alarm_actions = local.create_sns ? [aws_sns_topic.alarms[0].arn] : []

  # CloudWatch dimension for AWS/ApplicationELB metrics. Empty until the env feeds
  # module.alb.alb_arn_suffix, in which case the alarms scope to that LB.
  alb_dimensions = var.alb_arn_suffix != "" ? { LoadBalancer = var.alb_arn_suffix } : {}
}

resource "aws_cloudwatch_log_group" "this" {
  for_each = var.log_groups

  name              = "/inferenceops/${var.name}/${each.key}"
  retention_in_days = each.value

  tags = var.tags
}

resource "aws_sns_topic" "alarms" {
  count = local.create_sns ? 1 : 0

  name = "${var.name}-alarms"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  count = local.create_sns ? 1 : 0

  topic_arn = aws_sns_topic.alarms[0].arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_cloudwatch_metric_alarm" "http_5xx" {
  alarm_name        = "${var.name}-alb-5xx"
  alarm_description = "InferenceOps ${var.name}: ALB 5xx count >= ${var.http_5xx_threshold} over ${var.alarm_evaluation_periods} x ${var.alarm_period_seconds}s."

  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = var.alarm_evaluation_periods
  threshold           = var.http_5xx_threshold
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/ApplicationELB"
  metric_name = "HTTPCode_ELB_5XX_Count"
  statistic   = "Sum"
  period      = var.alarm_period_seconds

  dimensions = local.alb_dimensions

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "latency_p95" {
  alarm_name        = "${var.name}-latency-p95"
  alarm_description = "InferenceOps ${var.name}: ALB p95 latency > ${var.latency_p95_threshold_seconds}s over ${var.alarm_evaluation_periods} x ${var.alarm_period_seconds}s."

  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = var.alarm_evaluation_periods
  threshold           = var.latency_p95_threshold_seconds
  treat_missing_data  = "notBreaching"

  namespace          = "AWS/ApplicationELB"
  metric_name        = "TargetResponseTime"
  extended_statistic = "p95"
  period             = var.alarm_period_seconds

  dimensions = local.alb_dimensions

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = var.tags
}
