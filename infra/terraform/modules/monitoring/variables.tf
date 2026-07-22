variable "name" {
  description = "Environment name used in log-group paths (/inferenceops/<name>/<component>) and alarm names."
  type        = string
}

variable "log_groups" {
  description = "Map of component name -> retention in days. A log group /inferenceops/<name>/<component> is created for each entry."
  type        = map(number)
  default = {
    gateway      = 14
    model-server = 14
    worker       = 14
  }
}

variable "alarm_email" {
  description = "Email address subscribed to the alarms SNS topic. Leave empty (\"\") to create the alarms without any notification target."
  type        = string
  default     = ""
}

variable "alb_arn_suffix" {
  description = "ALB ARN suffix (module.alb.alb_arn_suffix) used as the LoadBalancer dimension on the ALB alarms. Leave empty to alarm across all load balancers."
  type        = string
  default     = ""
}

variable "alarm_period_seconds" {
  description = "Evaluation period length for both alarms, in seconds."
  type        = number
  default     = 300
}

variable "alarm_evaluation_periods" {
  description = "Number of consecutive breaching periods required to trigger an alarm."
  type        = number
  default     = 2
}

variable "http_5xx_threshold" {
  description = "5xx count per period that trips the error-rate alarm."
  type        = number
  default     = 10
}

variable "latency_p95_threshold_seconds" {
  description = "p95 target-response-time (seconds) that trips the latency alarm."
  type        = number
  default     = 0.5
}

variable "tags" {
  description = "Tags applied to every resource the module creates."
  type        = map(string)
  default     = {}
}
