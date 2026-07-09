variable "project_tag" {
  description = "Project tag applied to all resources"
  type        = string
}

variable "environment_tag" {
  description = "Environment tag applied to all resources (e.g. dev, staging, prod)"
  type        = string
}

variable "agent_name" {
  description = "Name of the frontend agent (from hierarchy.json)"
  type        = string
  default     = "supervisor"
}

variable "execution_role_arn" {
  description = "ARN of the IAM role for AgentCore Runtime execution. If empty, a new role will be created."
  type        = string
  default     = ""
}

variable "idp_type" {
  description = "Identity provider type: cognito or custom"
  type        = string
  default     = "cognito"
}

variable "cognito_user_pool_id" {
  description = "Cognito User Pool ID for JWT inbound auth"
  type        = string
  default     = ""
}

variable "cognito_app_client_id" {
  description = "Cognito App Client ID for JWT inbound auth"
  type        = string
  default     = ""
}

variable "custom_idp_issuer_url" {
  description = "Custom IdP issuer/discovery URL"
  type        = string
  default     = ""
}

variable "custom_idp_client_id" {
  description = "Custom IdP client ID"
  type        = string
  default     = ""
}

variable "container_image" {
  description = "Docker image URI for the agent container on AgentCore Runtime"
  type        = string
  default     = "public.ecr.aws/docker/library/python:3.12-slim"
}

variable "agent_registry_table_name" {
  description = "Name of the DynamoDB agent registry table"
  type        = string
  default     = "cloudops-agent-registry"
}

variable "agent_registry_table_arn" {
  description = "ARN of the DynamoDB agent registry table (for IAM permissions)"
  type        = string
  default     = ""
}

variable "agentcore_memory_id" {
  description = "AgentCore Memory resource ID (empty string disables memory)"
  type        = string
  default     = ""
}

variable "agentcore_gateway_endpoint" {
  description = "AgentCore Gateway endpoint URL"
  type        = string
  default     = ""
}

variable "agentcore_gateway_arn" {
  description = <<-EOT
    AgentCore Gateway ARN. Grants `bedrock-agentcore:InvokeGateway` to the
    frontend runtime role so a worker promoted to frontend
    (solo-leaf-as-frontend topology) can call gateway MCP tools directly.
    Harmless when the frontend is a mid-level orchestrator (the supervisor
    delegates to sub-agents and never calls the gateway itself) — the
    permission just goes unused.
  EOT
  type        = string
  default     = ""
}

variable "report_table_name" {
  description = "Name of the DynamoDB report-templates table"
  type        = string
  default     = ""
}

variable "report_table_arn" {
  description = "ARN of the DynamoDB report-templates table (for IAM permissions)"
  type        = string
  default     = ""
}

variable "bedrock_model_id" {
  description = "Default Bedrock model ID injected as BEDROCK_MODEL_ID env var. Empty string keeps the agent_base.py hardcoded fallback."
  type        = string
  default     = ""
}

variable "bedrock_guardrail_id" {
  description = "Bedrock Guardrail ID for input validation via ApplyGuardrail API"
  type        = string
  default     = ""
}

variable "bedrock_guardrail_version" {
  description = "Bedrock Guardrail version for input validation"
  type        = string
  default     = ""
}

variable "kms_key_arn" {
  description = "ARN of the KMS key used to encrypt DynamoDB tables the supervisor reads (registry, reports). Grants kms:Decrypt/GenerateDataKey so reads and writes to CMK-encrypted tables succeed. Empty = no KMS statement."
  type        = string
  default     = ""
}

variable "guardrail_mode" {
  description = "Guardrail enforcement mode: 'block' (default — refuse flagged input) or 'detect' (log-only, non-blocking). Detect mode lets operators observe what the guardrail would block without impacting traffic."
  type        = string
  default     = "block"
  validation {
    condition     = contains(["block", "detect"], var.guardrail_mode)
    error_message = "guardrail_mode must be 'block' or 'detect'."
  }
}

variable "redact_identifiers" {
  description = "Redact AWS identifiers — bare 12-digit account IDs and IAM/resource ARNs — from persisted output (memory + reports). Default false: this platform's reports are about the user's own resources, so blanking account IDs and ARNs makes 'top accounts by spend', over-permissioned-role findings, etc. meaningless. Access keys, external IDs, and role-session names are always redacted regardless."
  type        = bool
  default     = false
}
