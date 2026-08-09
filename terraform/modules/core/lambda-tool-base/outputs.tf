output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.this.arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.this.function_name
}

output "lambda_role_name" {
  description = "Name of the Lambda execution role (for feature modules that attach narrowly-scoped extra grants, e.g. snapshot-table reads)"
  value       = aws_iam_role.lambda.name
}
