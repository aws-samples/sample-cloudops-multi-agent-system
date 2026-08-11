variable "project_tag" {
  type = string
}

variable "environment_tag" {
  type = string
}

variable "collector_zip_path" {
  description = "Path to the collector Lambda zip file"
  type        = string
}

variable "tool_function_name" {
  description = "Name of the tag-governance MCP tool Lambda the collector invokes for scans"
  type        = string
}

variable "tool_role_name" {
  description = "Execution-role NAME of the tag-governance MCP tool Lambda — this module attaches the narrowly-scoped snapshot-table read grant to it"
  type        = string
}

variable "snapshot_schedule" {
  description = <<-EOT
    EventBridge schedule expression for the compliance sweep. Default is every
    6 hours — tag posture moves slowly, and each sweep costs a few tool-Lambda
    invocations. Tighten (e.g. rate(1 hour)) if the org is actively remediating
    and wants fresher numbers; users can always force a live scan per-request
    with force_refresh=true.
  EOT
  type        = string
  default     = "rate(6 hours)"
}

variable "snapshot_ttl_hours" {
  description = <<-EOT
    DynamoDB TTL on snapshot items, in hours. Acts as the staleness backstop:
    if the collector stops running (schedule disabled, repeated failures), the
    snapshot expires and the tool falls back to live queries rather than
    serving unboundedly-old data. Keep comfortably above the schedule period —
    default 48h vs the 6h schedule tolerates a weekend of failed runs.
  EOT
  type        = number
  default     = 48
}

variable "log_retention_days" {
  type    = number
  default = 30
}
