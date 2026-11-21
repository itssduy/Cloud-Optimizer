# Who can log in. Add people with:
#   aws cognito-idp admin-create-user --user-pool-id <id> --username someone@company.com
resource "aws_cognito_user_pool" "main" {
  name                     = local.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${local.name}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# optional: sign in with the company's login (Okta, Entra ID, Google Workspace, ...)
resource "aws_cognito_identity_provider" "corporate" {
  count         = var.oidc_issuer == "" ? 0 : 1
  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = var.oidc_provider_name
  provider_type = "OIDC"

  provider_details = {
    client_id                 = var.oidc_client_id
    client_secret             = var.oidc_client_secret
    oidc_issuer               = var.oidc_issuer
    authorize_scopes          = "openid email"
    attributes_request_method = "GET"
  }

  attribute_mapping = {
    email    = "email"
    username = "sub"
  }
}

# optional: sign in with IAM Identity Center (AWS SSO) or any SAML provider. See DEPLOY.md.
resource "aws_cognito_identity_provider" "saml" {
  count         = var.saml_metadata_url == "" ? 0 : 1
  user_pool_id  = aws_cognito_user_pool.main.id
  provider_name = var.saml_provider_name
  provider_type = "SAML"

  provider_details = {
    MetadataURL = var.saml_metadata_url
  }

  attribute_mapping = {
    email = "email"
  }
}

locals {
  # if a company login is set up, use only that (no Cognito passwords) and skip Cognito's chooser
  external_idps = concat(aws_cognito_identity_provider.saml[*].provider_name, aws_cognito_identity_provider.corporate[*].provider_name)
  login_idps    = length(local.external_idps) > 0 ? local.external_idps : ["COGNITO"]
  direct_idp    = length(local.external_idps) == 1 ? local.external_idps[0] : ""
}

resource "aws_cognito_user_pool_client" "app" {
  name            = "${local.name}-app"
  user_pool_id    = aws_cognito_user_pool.main.id
  generate_secret = true

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = local.login_idps
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]

  callback_urls = ["${local.app_url}/auth/callback/", "http://localhost:8000/auth/callback/"]
  logout_urls   = ["${local.app_url}/login/", "http://localhost:8000/login/"]
}

resource "aws_secretsmanager_secret" "cognito_client_secret" {
  name                    = "${local.name}/cognito-client-secret"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "cognito_client_secret" {
  secret_id     = aws_secretsmanager_secret.cognito_client_secret.id
  secret_string = aws_cognito_user_pool_client.app.client_secret
}

locals {
  cognito_domain = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
}
