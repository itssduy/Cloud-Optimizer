data "archive_file" "audit" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/audit"
  output_path = "${path.module}/build/audit.zip"
  excludes    = ["__pycache__"]
}

resource "aws_cloudwatch_log_group" "audit" {
  name              = "/aws/lambda/${local.name}-audit"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "audit" {
  function_name = "${local.name}-audit"
  role          = aws_iam_role.audit_lambda.arn
  runtime       = var.lambda_runtime
  handler       = "handler.lambda_handler"
  timeout       = 300
  memory_size   = 256

  filename         = data.archive_file.audit.output_path
  source_code_hash = data.archive_file.audit.output_base64sha256

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.main.name
      CUSTOMER_ROLE_NAME  = var.customer_role_name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.audit,
    aws_iam_role_policy_attachment.audit_lambda_logs,
  ]
}

resource "aws_cloudwatch_event_rule" "daily_audit" {
  name                = "${local.name}-daily-audit"
  description         = "Trigger the Cloud Optimizer audit Lambda daily"
  schedule_expression = var.audit_schedule_expression
  state               = var.enable_audit_schedule ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "daily_audit" {
  rule = aws_cloudwatch_event_rule.daily_audit.name
  arn  = aws_lambda_function.audit.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.audit.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_audit.arn
}
