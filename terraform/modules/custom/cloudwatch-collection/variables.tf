variable "project_tag" {
  type = string
}

variable "environment_tag" {
  type = string
}

variable "collector_zip_path" {
  type = string
}

variable "target_role_arn" {
  type    = string
  default = ""
}

variable "snapshot_schedule" {
  type    = string
  default = "rate(6 hours)"
}

variable "log_retention_days" {
  type    = number
  default = 30
}
