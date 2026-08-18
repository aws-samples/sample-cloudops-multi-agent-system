# -----------------------------------------------------------------------------
# Shared project config — mirrored to SSM Parameter Store so every developer
# pulling the repo picks up the same defaults without copy-pasting .env files.
#
# The authoritative source is still `terraform/config.auto.tfvars.json`, which
# Terraform auto-loads alongside `terraform.tfvars`. This module simply writes
# those values into SSM under `/$PROJECT/$ENV/config/*` so:
#
#   1. A second developer can `aws ssm get-parameters-by-path` to see what the
#      team is running with (used by `cmd_configure` to seed interactive
#      prompt defaults).
#   2. `terraform destroy` tears them down automatically — no orphan state.
#   3. Drift detection catches manual console edits.
#
# We are deliberately NOT reading these back into the runtime via SSM
# (Lambdas, agent runtimes). Runtime config flows through the existing
# env-var path populated from tfvars. SSM is purely for human/dev hydration.
# -----------------------------------------------------------------------------

locals {
  common_tags = {
    project     = var.project_tag
    environment = var.environment_tag
    managed_by  = "shared-config-module"
  }

  prefix = "/${var.project_tag}/${var.environment_tag}/config"

  # One entry per managed parameter. `type` defaults to String; the few
  # sensitive values use SecureString. `value` accepts empty strings —
  # an empty parameter is a valid "unset" signal and avoids having to
  # conditionally skip resources (which would break destroy / drift).
  parameters = {
    "aws_region"                                 = { type = "String", value = var.aws_region }
    "idp/type"                                   = { type = "String", value = var.idp_type }
    "idp/issuer_url"                             = { type = "String", value = var.custom_idp_issuer_url }
    "idp/client_id"                              = { type = "String", value = var.custom_idp_client_id }
    "idp/client_secret"                          = { type = "SecureString", value = var.custom_idp_client_secret }
    "app/url"                                    = { type = "String", value = var.app_url }
    "gateway/auth"                               = { type = "String", value = var.gateway_auth }
    "gateway/jwt_validation_claim"               = { type = "String", value = var.jwt_validation_claim }
    "cross_account/default_role_arn"             = { type = "String", value = var.cross_account_role_arn }
    "cross_account/coh_role_arn"                 = { type = "String", value = var.cross_account_role_arn_coh }
    "cross_account/tag_governance_role_arn"      = { type = "String", value = var.cross_account_role_arn_tag_governance }
    "cross_account/cloudwatch_role_arn"          = { type = "String", value = var.cross_account_role_arn_cloudwatch }
    "cross_account/health_role_arn"              = { type = "String", value = var.health_events_cross_account_role_arn }
    "cross_account/network_resilience_role_arns" = { type = "String", value = var.network_resilience_cross_account_role_arns }
    "cur/database_name"                          = { type = "String", value = var.cur_database_name }
    "cur/table_name"                             = { type = "String", value = var.cur_table_name }
    "cur/athena_workgroup"                       = { type = "String", value = var.athena_workgroup }
    "cur/athena_output_location"                 = { type = "String", value = var.athena_output_location }
    "observability/log_retention_days"           = { type = "String", value = tostring(var.observability_log_retention_days) }
    "deploy/mode"                                = { type = "String", value = var.deploy_mode }
    "deploy/tools"                               = { type = "String", value = var.deploy_tools }
    "deploy/agents"                              = { type = "String", value = var.deploy_agents }
    "model/default_id"                           = { type = "String", value = var.bedrock_model_id }
    "model/health_enrichment_id"                 = { type = "String", value = var.health_enrichment_model_id }
    "memory/id"                                  = { type = "String", value = var.memory_id }
  }
}

resource "aws_ssm_parameter" "this" {
  for_each = local.parameters

  name = "${local.prefix}/${each.key}"
  type = each.value.type
  # SSM requires at least 1 char. Use a single space sentinel for empty
  # values so we can keep the resource declared (and thus destroyable)
  # even when the user hasn't set the corresponding config. Readers treat
  # a single space the same as empty.
  value = each.value.value == "" ? " " : each.value.value

  tags = local.common_tags

  # A change in value is a routine user action — don't recreate on every
  # `terraform apply`, just update.
  lifecycle {
    create_before_destroy = false
  }
}
