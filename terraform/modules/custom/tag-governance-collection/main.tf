# Tag Governance Collection Module
#
# Scheduled snapshot pipeline for tag-compliance posture:
#   EventBridge schedule → collector Lambda → invoke tag-governance TOOL Lambda
#                                           → DynamoDB (snapshot table)
#
# Unlike health-events (event-driven: AWS pushes events onto the bus), tag
# compliance is a DERIVED scan — nothing emits "resource became non-compliant"
# — so this pipeline sweeps on a timer instead of consuming a queue. The
# collector re-uses the tool Lambda for all scan logic (policy resolution,
# Resource Explorer sweep, classification) so the two paths can never drift;
# it only orchestrates and stores.

locals {
  common_tags = {
    project     = var.project_tag
    environment = var.environment_tag
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# DynamoDB Table — snapshot cache
# ---------------------------------------------------------------------------
# Tiny table by design: one item per snapshotted operation (~5 rows) plus a
# LAST_RUN meta row. Generic pk/sk so a later per-resource layout (queryable
# compliance rows, GSIs) can move in without a table migration.
resource "aws_dynamodb_table" "tag_snapshots" {
  name         = "${var.project_tag}-tag-compliance-snapshots"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.project_tag}-tag-compliance-snapshots"
  })
}

# ---------------------------------------------------------------------------
# Snapshot read grant for the TOOL Lambda
# ---------------------------------------------------------------------------
# The tool's own policy (lambda-tool-base) is a single Action/Resource
# statement whose Resource must stay "*" for the scan APIs — it can't also
# scope DDB to one table. Attach the narrowly-scoped read here instead.
# No module cycle: this policy depends on the tool's ROLE, while the tool's
# FUNCTION depends on this module's table name — distinct resources.
resource "aws_iam_role_policy" "tool_snapshot_read" {
  name = "${var.project_tag}-tag-governance-snapshot-read"
  role = var.tool_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem"]
      Resource = aws_dynamodb_table.tag_snapshots.arn
    }]
  })
}

# ---------------------------------------------------------------------------
# Collector Lambda
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "collector" {
  name              = "/aws/lambda/${var.project_tag}-tag-governance-collector"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_iam_role" "collector" {
  name = "${var.project_tag}-tag-governance-collector-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "collector" {
  name = "${var.project_tag}-tag-governance-collector-policy"
  role = aws_iam_role.collector.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # The collector's ONLY data-plane permissions: write snapshots, and
        # invoke the tool Lambda that owns every AWS API the sweep needs.
        # Deliberately NO resource-explorer / tagging / organizations perms
        # here — one IAM surface (the tool's) for scan APIs.
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
        ]
        Resource = aws_dynamodb_table.tag_snapshots.arn
      },
      {
        # ARN constructed from the platform's deterministic tool naming
        # (lambda-tool-base: "<project>-<tool>-tool") rather than passed as a
        # module output — the tool module's env_vars reference THIS module's
        # table_name, so an output-based reference here would be a cycle.
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.tool_function_name}"
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
    ]
  })
}

resource "aws_lambda_function" "collector" {
  function_name    = "${var.project_tag}-tag-governance-collector"
  role             = aws_iam_role.collector.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  # The sweep serially invokes the tool for ~5 operations; check_tag_compliance
  # against a large estate can run tens of seconds per call. 300s is generous
  # headroom without allowing a runaway 15-min bill.
  timeout          = 300
  memory_size      = 256
  filename         = var.collector_zip_path
  source_code_hash = filebase64sha256(var.collector_zip_path)

  environment {
    variables = {
      TAG_SNAPSHOT_TABLE_NAME = aws_dynamodb_table.tag_snapshots.name
      TAG_TOOL_FUNCTION_NAME  = var.tool_function_name
      SNAPSHOT_TTL_HOURS      = tostring(var.snapshot_ttl_hours)
      LOG_LEVEL               = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.collector]

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge schedule
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.project_tag}-tag-governance-snapshot"
  description         = "Periodic tag-compliance snapshot sweep"
  schedule_expression = var.snapshot_schedule
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "collector" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.collector.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
