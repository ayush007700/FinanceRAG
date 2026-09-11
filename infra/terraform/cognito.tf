/*
 * Identity provider: a Cognito user pool, optional.
 *
 * The API validates any OIDC issuer's tokens and the UI runs PKCE against any
 * issuer, so nothing here is required -- an existing Auth0 or Entra tenant
 * works by setting auth_jwt in tfvars and the OIDC_* repository variables by
 * hand. This file exists so that a deployment with no IdP can get one from
 * the same apply, in the same account, with the claim names already lined up.
 *
 * Why the ID token, not the access token, is what the UI sends: Cognito's
 * access tokens carry `client_id` rather than `aud`, and omit custom
 * attributes. The ID token has `aud` = the app client id, the `custom:org_id`
 * attribute, and `cognito:groups`. The API pins audience, so the ID token is
 * the one that validates. That is a Cognito property, not a design choice,
 * and it is why the outputs below say OIDC_TOKEN=id.
 */

variable "enable_cognito" {
  type        = bool
  default     = false
  description = "Provision a Cognito user pool and wire the API and UI to it. Off by default: the API keys machine clients use need no IdP."
}

variable "cognito_domain_prefix" {
  type        = string
  default     = ""
  description = "Prefix for the Cognito-hosted login domain (<prefix>.auth.<region>.amazoncognito.com). Must be globally unique in the region; defaults to the project name."
}

locals {
  cognito_domain = var.cognito_domain_prefix != "" ? var.cognito_domain_prefix : var.project_name

  # Every callback the hosted UI may return to. The CloudFront UI is the real
  # one; localhost is for running `npm run dev` against the deployed API.
  cognito_callbacks = [
    "https://${aws_cloudfront_distribution.ui.domain_name}/",
    "http://localhost:3000/",
  ]

  # What the API needs, derived rather than copied, so enabling Cognito is
  # one flag rather than four values transcribed from outputs into tfvars.
  # An explicit auth_jwt in tfvars still wins: bring-your-own beats built-in.
  auth_jwt_effective = var.auth_jwt != null ? var.auth_jwt : (
    var.enable_cognito ? {
      jwks_url     = "${local.cognito_issuer}/.well-known/jwks.json"
      issuer       = local.cognito_issuer
      audience     = aws_cognito_user_pool_client.ui[0].id
      org_claim    = "custom:org_id"
      scopes_claim = "cognito:groups"
    } : null
  )

  cognito_issuer = var.enable_cognito ? "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.this[0].id}" : ""
}

resource "aws_cognito_user_pool" "this" {
  count = var.enable_cognito ? 1 : 0

  name = "${var.project_name}-users"

  # Email is the username. A separate username people must remember is one
  # more thing to reset.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # Where the tenant lives. Custom attributes surface in the ID token as
  # `custom:org_id`, which is the claim name the API is configured with above.
  # mutable=true so an admin can move a user between tenants without
  # recreating them.
  schema {
    name                     = "org_id"
    attribute_data_type      = "String"
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  # No self-service sign-up: this is an internal tool, and an account is
  # something an admin creates for a named person in a named tenant.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  lifecycle {
    # Users live here. A rename or attribute change must not destroy the pool.
    prevent_destroy = false
    ignore_changes  = [schema]
  }
}

# One group per API scope. Group membership becomes the `cognito:groups`
# claim, which the API reads as scopes -- so the group names must be exactly
# the scope names. A user in `ask` and `read` can question the corpus and see
# their audit trail; only someone in `index` can rewrite the corpus.
resource "aws_cognito_user_group" "scope" {
  for_each = var.enable_cognito ? toset(["ask", "index", "read", "metrics"]) : toset([])

  name         = each.key
  user_pool_id = aws_cognito_user_pool.this[0].id
  description  = "Grants the '${each.key}' API scope"
}

resource "aws_cognito_user_pool_client" "ui" {
  count = var.enable_cognito ? 1 : 0

  name         = "${var.project_name}-ui"
  user_pool_id = aws_cognito_user_pool.this[0].id

  # A public client: the browser cannot hold a secret, so none is issued.
  # PKCE is what stands in for it, and Cognito enforces it for code flow on
  # clients without a secret.
  generate_secret = false

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "profile", "email"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = local.cognito_callbacks
  logout_urls   = local.cognito_callbacks

  # One hour is the ceiling the UI is designed around: silent renew is off,
  # so this is how long a session lasts before signing in again.
  id_token_validity      = 60
  access_token_validity  = 60
  refresh_token_validity = 1
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }

  # Refuses to say whether a username exists on a failed login.
  prevent_user_existence_errors = "ENABLED"
}

resource "aws_cognito_user_pool_domain" "this" {
  count = var.enable_cognito ? 1 : 0

  domain       = local.cognito_domain
  user_pool_id = aws_cognito_user_pool.this[0].id
}

# ---------------------------------------------------------------------------
# Outputs: the four repository variables the UI build needs, and the values
# an operator needs to create the first user.
# ---------------------------------------------------------------------------

output "oidc_issuer" {
  description = "GitHub variable OIDC_ISSUER"
  value       = var.enable_cognito ? local.cognito_issuer : null
}

output "oidc_client_id" {
  description = "GitHub variable OIDC_CLIENT_ID"
  value       = var.enable_cognito ? aws_cognito_user_pool_client.ui[0].id : null
}

output "oidc_token" {
  description = "GitHub variable OIDC_TOKEN. 'id' because Cognito access tokens lack the aud claim the API pins."
  value       = var.enable_cognito ? "id" : null
}

output "cognito_user_pool_id" {
  description = "For `aws cognito-idp admin-create-user --user-pool-id`"
  value       = var.enable_cognito ? aws_cognito_user_pool.this[0].id : null
}

output "cognito_hosted_ui" {
  description = "The login page the UI redirects to"
  value       = var.enable_cognito ? "https://${local.cognito_domain}.auth.${var.aws_region}.amazoncognito.com" : null
}
