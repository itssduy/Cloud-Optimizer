resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "Django Fargate tasks"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "app_http" {
  for_each = toset(var.app_allowed_cidrs)

  security_group_id = aws_security_group.app.id
  cidr_ipv4         = each.value
  from_port         = var.container_port
  to_port           = var.container_port
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

moved {
  from = aws_vpc_security_group_ingress_rule.app_http
  to   = aws_vpc_security_group_ingress_rule.app_http["0.0.0.0/0"]
}
