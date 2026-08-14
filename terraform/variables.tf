variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "selected_agents" {
  description = "List of agent module names to deploy (e.g. [\"finops-agent\", \"governance-agent\"])"
  type        = list(string)
  default     = []
}

variable "agent_images" {
  description = "Map of agent name to ECR image URI"
  type        = map(string)
  default     = {}
}

variable "idp_type" {
  description = "Identity provider type: cognito or custom"
  type        = string
  default     = "cognito"

  validation {
    condition     = contains(["cognito", "custom"], var.idp_type)
    error_message = "idp_type must be one of: cognito, custom"
  }
}

variable "custom_idp_issuer_url" {
  description = "Custom OAuth2 identity provider issuer URL (used when idp_type is custom)"
  type        = string
  default     = ""
}

variable "custom_idp_client_id" {
  description = "Custom OAuth2 identity provider client ID (used when idp_type is custom)"
  type        = string
  default     = ""
}

variable "custom_idp_client_secret" {
  description = "Custom OAuth2 identity provider client secret (used when idp_type is custom)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "s3_bucket" {
  description = "S3 bucket name for Terraform state backend"
  type        = string
}

variable "dynamodb_table" {
  description = "DynamoDB table name for Terraform state locking"
  type        = string
}

variable "project_tag" {
  description = "Project tag applied to all resources"
  type        = string
  default     = "cloudops"
}

variable "environment_tag" {
  description = "Environment tag applied to all resources (e.g. dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "supervisor_url" {
  description = "URL of the Supervisor Agent invoke endpoint (injected into frontend config.json at deploy time)"
  type        = string
  default     = "http://localhost:9000/invoke"
}

variable "app_url" {
  description = "Public URL of the frontend app (CloudFront URL, set after first deploy)"
  type        = string
  default     = ""
}

variable "supervisor_image" {
  description = "Docker image URI for the Supervisor agent on AgentCore Runtime"
  type        = string
  default     = "public.ecr.aws/docker/library/python:3.12-slim"
}

# ---------------------------------------------------------------------------
# Deployment mode flags — control which components are deployed
# ---------------------------------------------------------------------------

variable "deploy_agents" {
  description = "Whether to deploy agent runtimes (supervisor + sub-agents)"
  type        = bool
  default     = true
}

variable "deploy_gateway" {
  description = "Whether to deploy the AgentCore Gateway"
  type        = bool
  default     = true
}

variable "deploy_tools" {
  description = "Whether to deploy Lambda MCP tools"
  type        = bool
  default     = true
}

variable "deploy_frontend" {
  description = "Whether to deploy the frontend (S3 + CloudFront)"
  type        = bool
  default     = true
}

variable "deploy_cognito" {
  description = "Whether to deploy Cognito user pool"
  type        = bool
  default     = true
}

variable "deploy_memory" {
  description = "Whether to deploy AgentCore Memory"
  type        = bool
  default     = true
}

variable "selected_tools" {
  description = "List of tool names to deploy (keys from tools.json). Empty = all."
  type        = list(string)
  default     = []
}

variable "enable_cloudwatch_coverage" {
  description = "Deploy the scheduled CloudWatch alarm coverage snapshot pipeline when the cloudwatch tool is selected."
  type        = bool
  default     = true
}

variable "cloudwatch_snapshot_schedule" {
  description = "EventBridge schedule for CloudWatch alarm coverage collection."
  type        = string
  default     = "rate(6 hours)"
}

variable "gateway_auth" {
  description = "Gateway auth type: iam (default) or oauth (Cognito JWT)"
  type        = string
  default     = "iam"

  validation {
    condition     = contains(["iam", "oauth"], var.gateway_auth)
    error_message = "gateway_auth must be one of: iam, oauth"
  }
}

variable "jwt_validation_claim" {
  description = <<-EOT
    Which JWT claim the OAuth gateway authorizer validates the Cognito client ID
    against. Only consulted when gateway_auth = "oauth"; ignored for iam.
      - "audience" (default): validate the `aud` claim. Use for callers that send
        Cognito ID tokens (e.g. the AG-UI frontend). This is the pre-existing
        behavior, kept as the default so upgrading an existing oauth deployment
        does not silently change validation.
      - "client": validate the `client_id` claim. Use for callers that send
        Cognito ACCESS tokens (e.g. Amazon Quick and OAuth resource-access
        MCP clients generally) — access tokens have no `aud` claim, so audience
        validation 403s them. `make configure` offers this as the default once
        you pick gateway_auth = oauth.
  EOT
  type        = string
  default     = "audience"

  validation {
    condition     = contains(["client", "audience"], var.jwt_validation_claim)
    error_message = "jwt_validation_claim must be one of: client, audience"
  }
}

variable "quick_oauth_callback_urls" {
  description = "Amazon Quick OAuth redirect URLs to allow-list on the Cognito app client when gateway_auth = oauth. Region-specific; defaults to the three Quick regions. Quick uses one of these as its Redirect URL in the MCP integration."
  type        = list(string)
  default = [
    "https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback",
    "https://us-west-2.quicksight.aws.amazon.com/sn/oauthcallback",
    "https://eu-west-1.quicksight.aws.amazon.com/sn/oauthcallback",
  ]
}

variable "tool_env_vars" {
  description = "Per-tool environment variables resolved from .env. Map of tool_name -> { key = value }."
  type        = map(map(string))
  default     = {}
}

variable "network_resilience_cross_account_role_arns" {
  description = "Optional spoke-account role ARNs for network-resilience-api's cross-account enrichment (Phase 7). Empty by default; AssumeRole permission is granted only for ARNs in this list."
  type        = list(string)
  default     = []
}

variable "health_events_cross_account_role_arn" {
  description = <<-EOT
    Optional IAM role ARN the health-events collector assumes before calling
    AWS Organizations (account-name resolution) and AWS Health org-view APIs
    (backfill against management-scope endpoints).

    Leave empty when the collector is deployed in the management account OR
    the Health-delegated-admin account — the Lambda uses its own execution
    role in that case. Set when the collector lives in an ops account that
    needs to reach out to a mgmt-scope role. Mirrors the CE/COH cross-
    account pattern; alias in shared/cross_account.py is HEALTH.
  EOT
  type        = string
  default     = ""
}

variable "bedrock_model_id" {
  description = <<-EOT
    Default Bedrock model ID for sub-agents. Mirrored to SSM at
    `/$project/$env/config/model/default_id` so every developer on the
    deployment picks up the same model. Per-agent overrides in
    hierarchy.json still win at runtime.

    Empty string keeps the historic hardcoded default in agent_base.py
    (us.anthropic.claude-sonnet-4-20250514-v1:0).
  EOT
  type        = string
  default     = ""
}

variable "guardrail_mode" {
  description = <<-EOT
    Bedrock Guardrail enforcement mode for the supervisor's user-input check:
      - "block"  (default): refuse prompts the guardrail flags (prompt attack,
                  sensitive info, denied topics) before they reach the model.
      - "detect": log what WOULD be blocked but allow the request through.
                  Useful to observe injection attempts without impacting
                  traffic before committing to block mode.
    Only the raw user message is screened — never system prompts.
  EOT
  type        = string
  default     = "block"
  validation {
    condition     = contains(["block", "detect"], var.guardrail_mode)
    error_message = "guardrail_mode must be 'block' or 'detect'."
  }
}

variable "redact_identifiers" {
  description = <<-EOT
    Redact AWS IDENTIFIERS — bare 12-digit account IDs and IAM/resource ARNs —
    from PERSISTED output (AgentCore Memory + saved reports). Default false:
    this platform's reports and findings are ABOUT the user's own resources
    (e.g. "Top accounts by spend", "which role is over-permissioned",
    tag-governance breakdowns), so blanking account IDs and ARNs makes that
    data meaningless. Set true for deployments that persist/share transcripts
    and want identifiers scrubbed too. Genuine SECRETS — access keys, external
    IDs, and role-session names — are ALWAYS redacted regardless of this flag.
  EOT
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = <<-EOT
    CloudWatch Logs retention (days) applied to all platform log groups:
    Lambda tools, frontend-api, vended runtime/gateway logs, and (via the
    post-deploy sweep) AgentCore's auto-created runtime groups. Mirrored to
    SSM `observability/log_retention_days` so the value is the single source
    of truth across Terraform-managed groups and the deploy.sh retention
    sweep. Recommended: 30 for operational logs, higher for audit retention.
  EOT
  type        = number
  default     = 30
}

variable "health_enrichment_model_id" {
  description = <<-EOT
    Bedrock model ID used by the health-events collector enrichment Lambda.
    Mirrored to SSM `model/health_enrichment_id`. Empty string disables LLM
    enrichment (the collector still writes rules-based fields).
  EOT
  type        = string
  default     = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "memory_id" {
  description = <<-EOT
    AgentCore memory ID. Auto-populated by `scripts/deploy.sh` post-memory
    creation (writes via `shared_config_set_value memory_id ...`) and
    mirrored to SSM `memory/id`. Do NOT set manually.
  EOT
  type        = string
  default     = ""
}

variable "enable_tag_snapshots" {
  description = <<-EOT
    Deploy the tag-governance collection module: a scheduled Lambda that
    snapshots org tag-compliance posture into DynamoDB so canonical
    (unfiltered) tag-governance queries answer in milliseconds instead of a
    3-60s live Resource Explorer sweep. Filtered queries and
    force_refresh=true still go live. Only takes effect when the
    tag-governance tool is deployed.
  EOT
  type        = bool
  default     = true
}

variable "tag_snapshot_schedule" {
  description = "EventBridge schedule expression for the tag-compliance snapshot sweep."
  type        = string
  default     = "rate(6 hours)"
}
