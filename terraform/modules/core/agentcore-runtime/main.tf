locals {
  common_tags = {
    project     = var.project_tag
    environment = var.environment_tag
  }

  create_execution_role = var.execution_role_arn == ""
  execution_role_arn    = local.create_execution_role ? aws_iam_role.agentcore_execution[0].arn : var.execution_role_arn
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# -----------------------------------------------------------------------------
# IAM Role for AgentCore Runtime Execution (created if not provided)
# -----------------------------------------------------------------------------
resource "aws_iam_role" "agentcore_execution" {
  count = local.create_execution_role ? 1 : 0

  name = "${var.project_tag}-agentcore-runtime-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = [
            "bedrock.amazonaws.com",
            "bedrock-agentcore.amazonaws.com"
          ]
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.project_tag}-agentcore-runtime-role"
  })
}

resource "aws_iam_role_policy" "agentcore_execution" {
  count = local.create_execution_role ? 1 : 0

  name = "${var.project_tag}-agentcore-runtime-policy"
  role = aws_iam_role.agentcore_execution[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:ApplyGuardrail",
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:us:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:guardrail/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "sts:AssumeRole",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      },
      # X-Ray trace export (ADOT). GetSamplingRules + GetSamplingTargets
      # are required by aws-opentelemetry-distro's sampler — ADOT refreshes
      # sampling decisions every ~10s via these APIs. Without them spans
      # get silently dropped and never reach aws/spans. See
      # docs/observability-tuning.md.
      {
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:Scan",
          "dynamodb:GetItem",
          "dynamodb:Query",
        ]
        # ARN is always populated — this module is gated on deploy_agents=true,
        # which is exactly when local.agent_registry_table_arn is non-empty.
        Resource = [var.agent_registry_table_arn]
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        # Scoped to the reports/templates table ONLY. No wildcard fallback —
        # the previous `table/*` branch was dead code (module exists only when
        # deploy_agents=true, when report_table_arn is always populated) and
        # would have widened writes to every table (incl. the agent registry).
        Resource = [var.report_table_arn]
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
        ]
        Resource = "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:runtime/*"
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateMemory",
          "bedrock-agentcore:GetMemory",
          "bedrock-agentcore:InvokeMemory",
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListSessions",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:DeleteSession",
        ]
        Resource = var.agentcore_memory_id != "" ? [
          "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:memory/${var.agentcore_memory_id}"
        ] : ["*"]
      },
      ],
      # Gateway invocation — required when a worker is promoted to frontend
      # (solo-leaf-as-frontend). The frontend runtime runs under THIS role
      # regardless of the promoted agent's original type, so it needs gateway
      # permission even though the baseline supervisor never uses it.
      # Scoped to the specific gateway ARN; harmless if unused.
      var.agentcore_gateway_arn != "" ? [{
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeGateway",
        ]
        Resource = var.agentcore_gateway_arn
      }] : [],
      # KMS access for the CMK-encrypted DynamoDB tables (registry + reports).
      # Decrypt is required to READ (registry scan, report GetItem); GenerateDataKey
      # is required to WRITE (report PutItem/UpdateItem). Without these, the
      # supervisor cannot read the registry (→ "no child agents") or persist
      # reports. Required on every role that touches a CMK-encrypted table.
      var.kms_key_arn != "" ? [{
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey",
        ]
        Resource = var.kms_key_arn
      }] : [],
    )
  })
}

# -----------------------------------------------------------------------------
# AgentCore Runtime — Native Resource (provider >= 6.18.0)
# -----------------------------------------------------------------------------
resource "aws_bedrockagentcore_agent_runtime" "this" {
  agent_runtime_name = replace("${var.project_tag}-runtime", "-", "_")

  network_configuration {
    network_mode = "PUBLIC"
  }

  agent_runtime_artifact {
    container_configuration {
      container_uri = var.container_image
    }
  }

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = var.idp_type == "cognito" ? "https://cognito-idp.${data.aws_region.current.region}.amazonaws.com/${var.cognito_user_pool_id}/.well-known/openid-configuration" : var.custom_idp_issuer_url
      allowed_audience = [var.idp_type == "cognito" ? var.cognito_app_client_id : var.custom_idp_client_id]
    }
  }

  # The supervisor is the user-facing entry point and serves the AG-UI
  # streaming protocol. AGUI became a native server_protocol enum in the
  # AWS provider v6.50.0 (previously only MCP, HTTP, A2A were accepted),
  # so Terraform now manages it directly — no post-deploy API workaround.
  protocol_configuration {
    server_protocol = "AGUI"
  }

  environment_variables = merge(
    {
      AGENT_NAME                 = var.agent_name
      AGENT_REGISTRY_TABLE       = var.agent_registry_table_name
      AGENTCORE_MEMORY_ID        = var.agentcore_memory_id
      AGENTCORE_GATEWAY_ENDPOINT = var.agentcore_gateway_endpoint
      AWS_REGION                 = data.aws_region.current.region
      AWS_DEFAULT_REGION         = data.aws_region.current.region
      REPORT_TABLE_NAME          = var.report_table_name
      REDACT_IDENTIFIERS         = var.redact_identifiers ? "true" : "false"
    },
    var.bedrock_model_id != "" ? { BEDROCK_MODEL_ID = var.bedrock_model_id } : {},
    var.bedrock_guardrail_id != "" ? {
      BEDROCK_GUARDRAIL_ID      = var.bedrock_guardrail_id
      BEDROCK_GUARDRAIL_VERSION = var.bedrock_guardrail_version
      GUARDRAIL_MODE            = var.guardrail_mode
    } : {}
  )

  role_arn = local.execution_role_arn

  tags = merge(local.common_tags, {
    Name = "${var.project_tag}-agentcore-runtime"
  })

  lifecycle {
    ignore_changes = [authorizer_configuration]
  }
}

# -----------------------------------------------------------------------------
# AgentCore Runtime Endpoint — tracks the latest runtime version
# -----------------------------------------------------------------------------
resource "aws_bedrockagentcore_agent_runtime_endpoint" "this" {
  name                  = replace("${var.project_tag}_endpoint", "-", "_")
  agent_runtime_id      = aws_bedrockagentcore_agent_runtime.this.agent_runtime_id
  agent_runtime_version = aws_bedrockagentcore_agent_runtime.this.agent_runtime_version

  tags = merge(local.common_tags, {
    Name = "${var.project_tag}-agentcore-runtime-endpoint"
  })
}

