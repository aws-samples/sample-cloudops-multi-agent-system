provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = var.project_tag
      environment = var.environment_tag
      managed_by  = "terraform"
      auto-delete = "no"
    }
  }
}

# -----------------------------------------------------------------------------
# Load JSON configs — single source of truth for agents and Lambda tools
# -----------------------------------------------------------------------------
locals {
  _raw_agent_configs = jsondecode(file("${path.module}/../src/agents/hierarchy.json"))
  _raw_tool_configs  = jsondecode(file("${path.module}/../src/lambda/mcp/tools.json"))

  # Find the frontend agent (type = "frontend"), default to "supervisor"
  _frontend_agents = [for k, v in local._raw_agent_configs : k if try(v.type, "") == "frontend"]
  frontend_agent   = length(local._frontend_agents) > 0 ? local._frontend_agents[0] : "supervisor"

  # Always use for-expressions to produce map types (avoids object type mismatch)
  agent_configs = { for k, v in local._raw_agent_configs : k => v }
  tool_configs  = { for k, v in local._raw_tool_configs : k => v }

  # Filter tools by selected_tools (empty = all)
  filtered_tools = { for k, v in local.tool_configs : k => v
    if length(var.selected_tools) == 0 || contains(var.selected_tools, k)
  }

  # Build parent map from children in hierarchy.json
  agent_parent_map = merge([
    for parent_name, parent_info in local.agent_configs : {
      for child in lookup(parent_info, "children", []) : child => parent_name
    }
  ]...)

  # Filter agents: exclude frontend agent (has its own runtime module), include only selected
  deployable_agents = { for k, v in local.agent_configs : k => v
    if var.deploy_agents && k != local.frontend_agent && contains(var.selected_agents, k)
  }

  # Determine mid-level agents (have children defined)
  mid_level_agents = toset([
    for k, v in local.agent_configs : k
    if k != local.frontend_agent && length(lookup(v, "children", [])) > 0
  ])
}

# -----------------------------------------------------------------------------
# Shared config — mirrors the values a user sets via `make configure` into
# SSM Parameter Store under /$project/$env/config/* so every developer pulling
# the repo sees the same defaults. Always deployed: it's the SSM-backed copy
# of what's already in config.auto.tfvars.json, not a source of truth the
# runtime reads from. `make destroy-all` cleans it up with the rest of the
# stack via `terraform destroy`.
# -----------------------------------------------------------------------------
module "shared_config" {
  source = "./modules/core/shared-config"

  project_tag     = var.project_tag
  environment_tag = var.environment_tag

  aws_region                            = var.aws_region
  idp_type                              = var.idp_type
  custom_idp_issuer_url                 = var.custom_idp_issuer_url
  custom_idp_client_id                  = var.custom_idp_client_id
  custom_idp_client_secret              = var.custom_idp_client_secret
  app_url                               = var.app_url
  gateway_auth                          = var.gateway_auth
  jwt_validation_claim                  = var.jwt_validation_claim
  cross_account_role_arn                = try(var.tool_env_vars["cost-explorer"]["CROSS_ACCOUNT_ROLE_ARN"], "")
  cross_account_role_arn_coh            = try(var.tool_env_vars["cost-optimization-hub"]["CROSS_ACCOUNT_ROLE_ARN_COH"], "")
  cross_account_role_arn_tag_governance = try(var.tool_env_vars["tag-governance"]["CROSS_ACCOUNT_ROLE_ARN_TAG_GOVERNANCE"], "")
  cross_account_role_arn_cloudwatch     = try(var.tool_env_vars["cloudwatch"]["CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH"], "")
  cur_database_name                     = try(var.tool_env_vars["cur-athena"]["CUR_DATABASE_NAME"], "")
  cur_table_name                        = try(var.tool_env_vars["cur-athena"]["CUR_TABLE_NAME"], "")
  athena_workgroup                      = try(var.tool_env_vars["cur-athena"]["ATHENA_WORKGROUP"], "")
  athena_output_location                = try(var.tool_env_vars["cur-athena"]["ATHENA_OUTPUT_LOCATION"], "")
  observability_log_retention_days      = var.log_retention_days
  deploy_mode                           = ""
  deploy_tools                          = join(",", var.selected_tools)
  deploy_agents                         = join(",", var.selected_agents)

  # Shared deployment policy values that used to live in tfvars-only or hardcoded
  # defaults. Mirrored to SSM so every developer on the same deployment picks up
  # the same model/memory/cross-account choices.
  bedrock_model_id                           = var.bedrock_model_id
  health_enrichment_model_id                 = var.health_enrichment_model_id
  health_events_cross_account_role_arn       = var.health_events_cross_account_role_arn
  network_resilience_cross_account_role_arns = join(",", var.network_resilience_cross_account_role_arns)
  memory_id                                  = var.memory_id
}

# -----------------------------------------------------------------------------
# Cognito (conditional — not needed for gateway-only/tools-only with IAM auth)
# -----------------------------------------------------------------------------
module "cognito" {
  source          = "./modules/core/cognito"
  count           = var.deploy_cognito ? 1 : 0
  project_tag     = var.project_tag
  environment_tag = var.environment_tag
  # Once `app_url` is known (post-first-deploy hook sets it from the
  # CloudFront URL), production allow-list carries only the real URL;
  # local dev adds/removes `http://localhost:3000/` at runtime via
  # run-local.sh's trap-based whitelist so it doesn't linger in prod.
  #
  # On the very first deploy, `app_url` is empty (CloudFront hasn't been
  # created yet). Cognito requires at least one callback URL when OAuth
  # code flow is enabled, so we seed with localhost as a bootstrap. The
  # post-apply hook in deploy.sh then updates `app_url` in
  # config.auto.tfvars.json and re-applies, which swaps localhost out.
  #
  # When gateway_auth = oauth (Quick integration), also allow-list Quick's
  # hosted OAuth redirect URLs and generate a client secret — Quick's user-auth
  # flow requires both. These are additive, so the frontend/localhost callbacks
  # still work for the AG-UI path.
  callback_urls = concat(
    var.app_url != "" ? ["${var.app_url}/callback/", "http://localhost:3000/"] : ["http://localhost:3000/"],
    var.gateway_auth == "oauth" ? var.quick_oauth_callback_urls : [],
  )
  logout_urls     = var.app_url != "" ? ["${var.app_url}/", "http://localhost:3000/"] : ["http://localhost:3000/"]
  generate_secret = var.gateway_auth == "oauth"
  # "phone" is requested by Quick's OAuth flow but unused by the AG-UI app —
  # only allow-list it on the oauth path so the iam/frontend client is unchanged.
  extra_oauth_scopes = var.gateway_auth == "oauth" ? ["phone"] : []
}

locals {
  cognito_user_pool_id  = var.deploy_cognito ? module.cognito[0].user_pool_id : ""
  cognito_app_client_id = var.deploy_cognito ? module.cognito[0].app_client_id : ""
}

# -----------------------------------------------------------------------------
# KMS — Customer-Managed Key for DynamoDB + CloudWatch Logs encryption
# -----------------------------------------------------------------------------
module "kms" {
  source          = "./modules/core/kms"
  project_tag     = var.project_tag
  environment_tag = var.environment_tag
}

# -----------------------------------------------------------------------------
# Bedrock Guardrail — prompt attack detection + sensitive info filtering
# -----------------------------------------------------------------------------
module "guardrail" {
  source          = "./modules/core/guardrail"
  project_tag     = var.project_tag
  environment_tag = var.environment_tag
}

# -----------------------------------------------------------------------------
# DynamoDB (needed for agents — registry, reports, templates)
# -----------------------------------------------------------------------------
module "dynamodb" {
  source          = "./modules/core/dynamodb"
  count           = var.deploy_agents ? 1 : 0
  project_tag     = var.project_tag
  environment_tag = var.environment_tag
  kms_key_arn     = module.kms.key_arn
}

locals {
  agent_registry_table_name = var.deploy_agents ? module.dynamodb[0].agent_registry_table_name : ""
  agent_registry_table_arn  = var.deploy_agents ? module.dynamodb[0].agent_registry_table_arn : ""
  report_table_name         = var.deploy_agents ? module.dynamodb[0].report_templates_table_name : ""
  report_table_arn          = var.deploy_agents ? module.dynamodb[0].report_templates_table_arn : ""
}

# -----------------------------------------------------------------------------
# Health Events Data Collection (only when health-events tool is deployed)
# -----------------------------------------------------------------------------
module "health_events_collection" {
  source                 = "./modules/custom/health-events-collection"
  count                  = var.deploy_tools && contains(var.selected_tools, "health-events") || (var.deploy_tools && length(var.selected_tools) == 0) ? 1 : 0
  project_tag            = var.project_tag
  environment_tag        = var.environment_tag
  collector_zip_path     = "${path.module}/../src/lambda/collectors/health-events/health-events-collector.zip"
  cross_account_role_arn = var.health_events_cross_account_role_arn
  enrichment_model_id    = var.health_enrichment_model_id != "" ? var.health_enrichment_model_id : "global.anthropic.claude-haiku-4-5-20251001-v1:0"
}

# -----------------------------------------------------------------------------
# Frontend API (only when agents + frontend are deployed)
# -----------------------------------------------------------------------------
module "frontend_api" {
  source                = "./modules/core/frontend-api"
  count                 = var.deploy_agents && var.deploy_frontend ? 1 : 0
  project_tag           = var.project_tag
  environment_tag       = var.environment_tag
  lambda_zip_path       = "${path.module}/../src/lambda/frontend/core-api.zip"
  cognito_user_pool_id  = local.cognito_user_pool_id
  cognito_app_client_id = local.cognito_app_client_id
  agentcore_memory_id   = local.memory_id
  report_table_name     = local.report_table_name
  report_table_arn      = local.report_table_arn
  allowed_origins       = compact([var.app_url != "" ? var.app_url : "", "http://localhost:3000"])
  kms_key_arn           = module.kms.key_arn
  log_retention_days    = var.log_retention_days
}

# -----------------------------------------------------------------------------
# network-resilience-api — browser-facing REST Lambda, attaches routes to the
# same API Gateway as frontend-api. Gated on network-resiliency-agent being in
# the selected-agents list AND the frontend-api module being up (that's what
# creates the API Gateway + Cognito authorizer we reuse).
# -----------------------------------------------------------------------------
module "network_resilience_api" {
  source = "./modules/custom/network-resilience-api"
  count = (
    var.deploy_agents
    && var.deploy_frontend
    && contains(var.selected_agents, "network-resiliency-agent")
  ) ? 1 : 0

  project_tag     = var.project_tag
  environment_tag = var.environment_tag
  lambda_zip_path = "${path.module}/../src/lambda/frontend/network-resilience.zip"

  api_gateway_id            = module.frontend_api[0].api_id
  api_gateway_execution_arn = module.frontend_api[0].api_execution_arn
  cognito_authorizer_id     = module.frontend_api[0].cognito_authorizer_id

  cross_account_role_arns = var.network_resilience_cross_account_role_arns
}

# -----------------------------------------------------------------------------
# AgentCore Runtime (Supervisor — only when agents are deployed)
# -----------------------------------------------------------------------------
module "agentcore_runtime" {
  source                = "./modules/core/agentcore-runtime"
  count                 = var.deploy_agents ? 1 : 0
  agent_name            = local.frontend_agent
  container_image       = var.supervisor_image
  idp_type              = var.idp_type
  cognito_user_pool_id  = local.cognito_user_pool_id
  cognito_app_client_id = local.cognito_app_client_id
  custom_idp_issuer_url = var.custom_idp_issuer_url
  custom_idp_client_id  = var.custom_idp_client_id
  project_tag           = var.project_tag
  environment_tag       = var.environment_tag

  agent_registry_table_name  = local.agent_registry_table_name
  agent_registry_table_arn   = local.agent_registry_table_arn
  report_table_name          = local.report_table_name
  report_table_arn           = local.report_table_arn
  agentcore_memory_id        = local.memory_id
  agentcore_gateway_endpoint = local.gateway_endpoint
  agentcore_gateway_arn      = local.gateway_arn
  bedrock_model_id           = var.bedrock_model_id
  bedrock_guardrail_id       = module.guardrail.guardrail_id
  bedrock_guardrail_version  = module.guardrail.guardrail_version
  guardrail_mode             = var.guardrail_mode
  redact_identifiers         = var.redact_identifiers
  kms_key_arn                = module.kms.key_arn
}

# -----------------------------------------------------------------------------
# AgentCore Memory (only when agents are deployed)
# -----------------------------------------------------------------------------
module "agentcore_memory" {
  source          = "./modules/core/agentcore-memory"
  count           = var.deploy_memory ? 1 : 0
  project_tag     = var.project_tag
  environment_tag = var.environment_tag
}

locals {
  memory_id = var.deploy_memory ? module.agentcore_memory[0].memory_id : ""
}

# -----------------------------------------------------------------------------
# Lambda Tools — filtered by selected_tools, gated by deploy_tools
# -----------------------------------------------------------------------------
module "lambda_tools" {
  source   = "./modules/core/lambda-tool-base"
  for_each = { for k, v in local.filtered_tools : k => v if var.deploy_tools }

  tool_name       = each.key
  project_tag     = var.project_tag
  environment_tag = var.environment_tag
  lambda_zip_path = "${path.module}/../src/lambda/mcp/${each.key}.zip"
  handler         = each.value.handler
  runtime         = each.value.runtime
  timeout         = each.value.timeout
  memory_size     = each.value.memory
  iam_actions     = each.value.iam_actions

  # Tools that need DynamoDB table-specific IAM resources surface that
  # need via a `needs_*` flag in tools.json — see health-events.
  iam_resources = lookup(each.value, "needs_health_events", false) && length(module.health_events_collection) > 0 ? [
    module.health_events_collection[0].table_arn,
    "${module.health_events_collection[0].table_arn}/index/*"
  ] : ["*"]

  # Env vars: merge infra-derived (needs_* flags) with user-provided (tool_env_vars from .env)
  env_vars = merge(
    # User-provided env vars from tools.json -> .env
    lookup(var.tool_env_vars, each.key, {}),
    # Infra-derived env vars (override user-provided if both set)
    lookup(each.value, "needs_health_events", false) && length(module.health_events_collection) > 0 ? {
      HEALTH_EVENTS_TABLE_NAME = module.health_events_collection[0].table_name
    } : {},
  )

  kms_key_arn        = module.kms.key_arn
  log_retention_days = var.log_retention_days
}

# -----------------------------------------------------------------------------
# AgentCore Gateway (gated by deploy_gateway, supports IAM or OAuth auth)
# -----------------------------------------------------------------------------
module "agentcore_gateway" {
  source = "./modules/core/agentcore-gateway"
  count  = var.deploy_gateway ? 1 : 0

  project_tag     = var.project_tag
  environment_tag = var.environment_tag
  gateway_auth    = var.gateway_auth

  # OAuth config (only used when gateway_auth = "oauth")
  cognito_user_pool_id  = local.cognito_user_pool_id
  cognito_app_client_id = local.cognito_app_client_id
  jwt_validation_claim  = var.jwt_validation_claim

  lambda_tool_arns = {
    for k, v in module.lambda_tools : k => v.lambda_function_arn
  }
}

locals {
  gateway_endpoint = var.deploy_gateway ? module.agentcore_gateway[0].gateway_endpoint : ""
  gateway_arn      = var.deploy_gateway ? module.agentcore_gateway[0].gateway_arn : ""
  gateway_id       = var.deploy_gateway ? module.agentcore_gateway[0].gateway_id : ""
}

# -----------------------------------------------------------------------------
# Frontend (gated by deploy_frontend)
# -----------------------------------------------------------------------------
module "frontend" {
  source          = "./modules/core/frontend"
  count           = var.deploy_frontend ? 1 : 0
  project_tag     = var.project_tag
  environment_tag = var.environment_tag
}

# -----------------------------------------------------------------------------
# Observability (only when agents are deployed)
# -----------------------------------------------------------------------------
module "observability" {
  source                 = "./modules/core/observability"
  count                  = var.deploy_agents ? 1 : 0
  project_tag            = var.project_tag
  environment_tag        = var.environment_tag
  agent_names            = keys(local.agent_configs)
  runtime_arn            = try(module.agentcore_runtime[0].runtime_arn, "")
  gateway_arn            = local.gateway_arn
  memory_arn             = try(module.agentcore_memory[0].memory_arn, "")
  enable_runtime_tracing = var.deploy_agents
  enable_gateway_tracing = var.deploy_gateway
  enable_memory_tracing  = var.deploy_memory
  kms_key_arn            = module.kms.key_arn
  log_retention_days     = var.log_retention_days
}

# -----------------------------------------------------------------------------
# Agent Runtimes — gated by deploy_agents, filtered by selected_agents
# -----------------------------------------------------------------------------
module "agents" {
  source   = "./modules/core/agent-runtime-base"
  for_each = local.deployable_agents

  enabled                    = true
  container_image            = lookup(var.agent_images, each.key, "")
  project_tag                = var.project_tag
  environment_tag            = var.environment_tag
  agent_name                 = each.key
  agent_description          = lookup(each.value, "description", "")
  parent_agent               = lookup(local.agent_parent_map, each.key, "")
  agent_registry_table_name  = local.agent_registry_table_name
  agent_registry_table_arn   = local.agent_registry_table_arn
  agentcore_gateway_endpoint = local.gateway_endpoint
  agentcore_gateway_arn      = local.gateway_arn
  agentcore_memory_id        = local.memory_id
  is_mid_level               = contains(local.mid_level_agents, each.key)
  bedrock_model_id           = var.bedrock_model_id
  kms_key_arn                = module.kms.key_arn
}
