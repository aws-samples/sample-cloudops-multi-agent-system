variable "project_tag" {
  description = "Project prefix — forms the first segment of the SSM path."
  type        = string
}

variable "environment_tag" {
  description = "Environment (dev/staging/prod) — forms the second segment of the SSM path."
  type        = string
}

# ---------------------------------------------------------------------------
# Shared project values managed here so every developer pulling the repo
# sees the same settings without copying .env files. Unset values land in
# SSM as empty strings; callers that care (Lambda handlers, runtime env
# builders) treat empty as "not configured".
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for all resources (mirrored into SSM so other devs see the same value)."
  type        = string
}

variable "idp_type" {
  description = "Identity provider — cognito or custom."
  type        = string
  default     = "cognito"
}

variable "custom_idp_issuer_url" {
  description = "OIDC issuer URL when idp_type = custom."
  type        = string
  default     = ""
}

variable "custom_idp_client_id" {
  description = "OIDC client id when idp_type = custom."
  type        = string
  default     = ""
}

variable "custom_idp_client_secret" {
  description = "OIDC client secret when idp_type = custom. Stored as SecureString."
  type        = string
  default     = ""
  sensitive   = true
}

variable "app_url" {
  description = "Public URL the frontend is served at (e.g. CloudFront URL). Auto-populated post-deploy."
  type        = string
  default     = ""
}

variable "gateway_auth" {
  description = "Gateway authorizer mode — iam or oauth."
  type        = string
  default     = "iam"
}

variable "jwt_validation_claim" {
  description = "OAuth gateway JWT claim to validate the Cognito client ID against — audience (ID tokens, AG-UI) or client (access tokens, Quick). Only used when gateway_auth = oauth."
  type        = string
  default     = "audience"
}

variable "cross_account_role_arn" {
  description = "Default cross-account role ARN used by MCP tools with CROSS_ACCOUNT_ROLE_ARN env var."
  type        = string
  default     = ""
}

variable "cross_account_role_arn_coh" {
  description = "Cost Optimization Hub cross-account role ARN (typically the delegated admin account)."
  type        = string
  default     = ""
}

variable "cross_account_role_arn_tag_governance" {
  description = "Tag Governance cross-account role ARN — used when the tag-governance Lambda deploys outside the management account and needs to assume into the payer for GetComplianceSummary / ListCostAllocationTags / DescribeEffectivePolicy."
  type        = string
  default     = ""
}

variable "cross_account_role_arn_cloudwatch" {
  description = "CloudWatch cross-account role ARN — assumed by the cloudwatch MCP Lambda when reading metrics, alarms, and history from a spoke account. Mirrored to SSM at /<project>/<env>/config/cross_account/cloudwatch_role_arn per Requirement 6.4. Empty falls back to the Lambda execution role per Requirement 6.3."
  type        = string
  default     = ""
}

variable "cur_database_name" {
  description = "Glue database for CUR data."
  type        = string
  default     = ""
}

variable "cur_table_name" {
  description = "Glue table for CUR data."
  type        = string
  default     = ""
}

variable "athena_workgroup" {
  description = "Athena workgroup used by the cur-athena MCP tool."
  type        = string
  default     = ""
}

variable "athena_output_location" {
  description = "S3 URI for Athena query results."
  type        = string
  default     = ""
}

variable "observability_log_retention_days" {
  description = "CloudWatch log retention for observability-managed log groups."
  type        = number
  default     = 14
}

variable "deploy_mode" {
  description = "Last-used DEPLOY_MODE (recorded for team awareness; per-invocation env var always wins)."
  type        = string
  default     = "full"
}

variable "deploy_tools" {
  description = "Comma-separated tool filter (empty = all)."
  type        = string
  default     = ""
}

variable "deploy_agents" {
  description = "Comma-separated agent filter (empty = all)."
  type        = string
  default     = ""
}

variable "bedrock_model_id" {
  description = "Default Bedrock model ID for sub-agents (per-agent overrides still allowed via hierarchy.json)."
  type        = string
  default     = ""
}

variable "health_enrichment_model_id" {
  description = "Bedrock model ID for the health-events collector's narrative enrichment Lambda. Empty = disable enrichment."
  type        = string
  default     = ""
}

variable "health_events_cross_account_role_arn" {
  description = "Optional cross-account role ARN the health-events collector assumes before calling Organizations / AWS Health org-view APIs. Mirrored to SSM cross_account/health_role_arn."
  type        = string
  default     = ""
}

variable "network_resilience_cross_account_role_arns" {
  description = "Comma-separated spoke-account role ARNs for the network-resilience-api enrichment path. Mirrored to SSM cross_account/network_resilience_role_arns."
  type        = string
  default     = ""
}

variable "memory_id" {
  description = "AgentCore memory ID — auto-populated by the deploy script after memory creation, mirrored to SSM memory/id."
  type        = string
  default     = ""
}
