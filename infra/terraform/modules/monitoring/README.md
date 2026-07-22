# monitoring module

Observability baseline for one InferenceOps environment:

- **Log groups** — one per platform component (`gateway`, `model-server`,
  `worker` by default) at `/inferenceops/<name>/<component>` with configurable
  retention.
- **SLO alarms** on the ALB created by the [`alb`](../alb/) module:
  - `http_5xx` — `HTTPCode_ELB_5XX_Count` (Sum) ≥ threshold.
  - `latency_p95` — `TargetResponseTime` p95 > threshold.
  Both use `treat_missing_data = "notBreaching"` so an idle environment never
  pages.
- **Optional SNS email notification** — set `alarm_email` to subscribe one
  address; leave `""` to create the alarms without a notification target.

Plain resources only (no data sources), so it plans identically against real
AWS and the offline moto endpoint (todo 24).

## Usage

```hcl
module "monitoring" {
  source = "../../modules/monitoring"

  name           = "inferenceops-dev"
  alarm_email    = "oncall@example.com" # "" disables SNS
  alb_arn_suffix = module.alb.alb_arn_suffix
}
```

## Variables

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `name` | `string` | — | Environment name (log-group path + alarm names). |
| `log_groups` | `map(number)` | `{gateway=14, model-server=14, worker=14}` | Component → retention days. |
| `alarm_email` | `string` | `""` | SNS email subscriber; `""` = no notifications. |
| `alb_arn_suffix` | `string` | `""` | `LoadBalancer` metric dimension (`module.alb.alb_arn_suffix`). |
| `alarm_period_seconds` | `number` | `300` | Alarm period. |
| `alarm_evaluation_periods` | `number` | `2` | Breaching periods to trigger. |
| `http_5xx_threshold` | `number` | `10` | 5xx count threshold per period. |
| `latency_p95_threshold_seconds` | `number` | `0.5` | p95 latency threshold (s). |
| `tags` | `map(string)` | `{}` | Tags for all resources. |

## Outputs

| Name | Description |
|------|-------------|
| `log_group_names` | Component → log group name. |
| `log_group_arns` | Component → log group ARN. |
| `alarm_arns` | `{ http_5xx, latency_p95 }` → alarm ARN. |
| `sns_topic_arn` | Alarms topic ARN, or `null` when `alarm_email` is empty. |
