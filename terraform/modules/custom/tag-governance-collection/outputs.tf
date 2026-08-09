output "table_name" {
  value = aws_dynamodb_table.tag_snapshots.name
}

output "table_arn" {
  value = aws_dynamodb_table.tag_snapshots.arn
}

output "collector_function_name" {
  value = aws_lambda_function.collector.function_name
}
