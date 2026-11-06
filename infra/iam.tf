data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# let us assume the role customers make in their account
data "aws_iam_policy_document" "assume_customer_roles" {
  statement {
    actions   = ["sts:AssumeRole"]
    resources = [local.customer_role_arn_pattern]
  }
}

data "aws_iam_policy_document" "dynamodb_rw" {
  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:ConditionCheckItem",
      "dynamodb:DescribeTable",
    ]
    resources = [
      aws_dynamodb_table.main.arn,
      "${aws_dynamodb_table.main.arn}/index/*",
    ]
  }
}

# ECS uses this one to pull the image and read secrets

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.django_secret_key.arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "read-app-secrets"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

# the Django app runs as this role

resource "aws_iam_role" "app_task" {
  name               = "${local.name}-app-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "app_task" {
  source_policy_documents = [
    data.aws_iam_policy_document.assume_customer_roles.json,
    data.aws_iam_policy_document.dynamodb_rw.json,
  ]
}

resource "aws_iam_role_policy" "app_task" {
  name   = "app"
  role   = aws_iam_role.app_task.id
  policy = data.aws_iam_policy_document.app_task.json
}

# audit lambda

resource "aws_iam_role" "audit_lambda" {
  name               = "${local.name}-audit-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "audit_lambda_logs" {
  role       = aws_iam_role.audit_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "audit_lambda" {
  source_policy_documents = [
    data.aws_iam_policy_document.assume_customer_roles.json,
    data.aws_iam_policy_document.dynamodb_rw.json,
  ]
}

resource "aws_iam_role_policy" "audit_lambda" {
  name   = "audit"
  role   = aws_iam_role.audit_lambda.id
  policy = data.aws_iam_policy_document.audit_lambda.json
}
