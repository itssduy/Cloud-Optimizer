variable "project_name" {
  description = "Used in resource names"
  type        = string
  default     = "cloud-optimizer"
}

variable "environment" {
  description = "dev, prod, etc."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "container_port" {
  description = "Port Django listens on"
  type        = number
  default     = 8000
}

variable "app_image_tag" {
  description = "Image tag to deploy"
  type        = string
  default     = "latest"
}

variable "app_desired_count" {
  description = "Number of app tasks, leave at 0 until an image is pushed"
  type        = number
  default     = 0
}

variable "app_cpu" {
  description = "Task CPU"
  type        = number
  default     = 256
}

variable "app_memory" {
  description = "Task memory in MiB"
  type        = number
  default     = 512
}

variable "allowed_hosts" {
  description = "DJANGO_ALLOWED_HOSTS, the task IP changes on every deploy so this is * for now"
  type        = list(string)
  default     = ["*"]
}

variable "log_retention_days" {
  description = "Days to keep logs"
  type        = number
  default     = 14
}

variable "dynamodb_point_in_time_recovery" {
  description = "Point-in-time recovery for the table"
  type        = bool
  default     = true
}

variable "dynamodb_deletion_protection" {
  description = "Has to be false to destroy the table"
  type        = bool
  default     = false
}

variable "lambda_runtime" {
  description = "Lambda runtime"
  type        = string
  default     = "python3.13"
}

variable "audit_schedule_expression" {
  description = "When the audit runs (UTC)"
  type        = string
  default     = "cron(0 6 * * ? *)"
}

variable "enable_audit_schedule" {
  description = "Turn the daily audit on/off"
  type        = bool
  default     = true
}
