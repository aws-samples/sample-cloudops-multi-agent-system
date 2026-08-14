locals {
  name             = "${var.project_tag}-cloudwatch-coverage"
  metric_namespace = "CloudOps/CloudWatchCoverage"
  common_tags = {
    project     = var.project_tag
    environment = var.environment_tag
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_dynamodb_table" "coverage" {
  name         = local.name
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

  tags = local.common_tags
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-dlq"
  message_retention_seconds = 1209600
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${local.name}-jobs"
  visibility_timeout_seconds = 960
  message_retention_seconds  = 1209600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "coordinator" {
  name              = "/aws/lambda/${local.name}-coordinator"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name}-worker"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_iam_role" "coordinator" {
  name = "${local.name}-coordinator-role"
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

resource "aws_iam_role" "worker" {
  name = "${local.name}-worker-role"
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

resource "aws_iam_role_policy" "coordinator" {
  name = "${local.name}-coordinator"
  role = aws_iam_role.coordinator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.coverage.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.jobs.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeRegions", "sts:GetCallerIdentity"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = local.metric_namespace
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.coordinator.arn}:*"
      }
      ], var.target_role_arn != "" ? [{
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = var.target_role_arn
      }] : []
    )
  })
}

resource "aws_iam_role_policy" "worker" {
  name = "${local.name}-worker"
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:BatchWriteItem",
        ]
        Resource = aws_dynamodb_table.coverage.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:SendMessage",
        ]
        Resource = aws_sqs_queue.jobs.arn
      },
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "resource-explorer-2:GetDefaultView",
          "resource-explorer-2:ListResources",
          "resource-explorer-2:Search",
          "tag:GetResources",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = local.metric_namespace
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.worker.arn}:*"
      }
      ], var.target_role_arn != "" ? [{
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = var.target_role_arn
      }] : []
    )
  })
}

resource "aws_lambda_function" "coordinator" {
  function_name    = "${local.name}-coordinator"
  role             = aws_iam_role.coordinator.arn
  handler          = "coordinator.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 256
  filename         = var.collector_zip_path
  source_code_hash = filebase64sha256(var.collector_zip_path)

  environment {
    variables = {
      CLOUDWATCH_COVERAGE_TABLE_NAME       = aws_dynamodb_table.coverage.name
      CLOUDWATCH_COVERAGE_QUEUE_URL        = aws_sqs_queue.jobs.url
      CLOUDWATCH_COVERAGE_METRIC_NAMESPACE = local.metric_namespace
      CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH    = var.target_role_arn
      LOG_LEVEL                            = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.coordinator]
  tags       = local.common_tags
}

resource "aws_lambda_function" "worker" {
  function_name    = "${local.name}-worker"
  role             = aws_iam_role.worker.arn
  handler          = "worker.handler"
  runtime          = "python3.12"
  timeout          = 900
  memory_size      = 1024
  filename         = var.collector_zip_path
  source_code_hash = filebase64sha256(var.collector_zip_path)

  environment {
    variables = {
      CLOUDWATCH_COVERAGE_TABLE_NAME       = aws_dynamodb_table.coverage.name
      CLOUDWATCH_COVERAGE_QUEUE_URL        = aws_sqs_queue.jobs.url
      CLOUDWATCH_COVERAGE_METRIC_NAMESPACE = local.metric_namespace
      RESOURCE_EXPLORER_AGGREGATOR_REGION  = data.aws_region.current.region
      CROSS_ACCOUNT_ROLE_ARN_CLOUDWATCH    = var.target_role_arn
      LOG_LEVEL                            = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.worker]
  tags       = local.common_tags
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1
}

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = local.name
  schedule_expression = var.snapshot_schedule
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "coordinator" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.coordinator.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.coordinator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

resource "aws_cloudwatch_metric_alarm" "no_snapshot" {
  alarm_name          = "${local.name}-no-success-12h"
  namespace           = local.metric_namespace
  metric_name         = "SnapshotPublished"
  statistic           = "Sum"
  period              = 43200
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  alarm_name  = "${local.name}-dlq"
  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions = {
    QueueName = aws_sqs_queue.dlq.name
  }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "worker_failures" {
  alarm_name          = "${local.name}-worker-failures"
  namespace           = local.metric_namespace
  metric_name         = "WorkerErrors"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}
