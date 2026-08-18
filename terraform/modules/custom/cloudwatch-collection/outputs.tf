output "table_name" {
  value = aws_dynamodb_table.coverage.name
}

output "table_arn" {
  value = aws_dynamodb_table.coverage.arn
}

output "coordinator_function_name" {
  value = aws_lambda_function.coordinator.function_name
}

output "coordinator_function_arn" {
  value = aws_lambda_function.coordinator.arn
}

output "queue_arn" {
  value = aws_sqs_queue.jobs.arn
}
