# Secrets: never bake credentials into the Docker image.
# ECS injects them at runtime from AWS Systems Manager Parameter Store.

resource "aws_ssm_parameter" "openai_api_key" {
  name  = "/${var.project_name}/OPENAI_API_KEY"
  type  = "SecureString"
  value = var.openai_api_key
}

# Full SQLAlchemy URL including the password, so the task needs exactly one
# secret for database access.
resource "aws_ssm_parameter" "database_url" {
  name  = "/${var.project_name}/DATABASE_URL"
  type  = "SecureString"
  value = local.database_url
}

# Optional: only created when a key is supplied. SSM rejects empty values, so an
# unset key must not create the parameter at all.
resource "aws_ssm_parameter" "cohere_api_key" {
  count = var.cohere_api_key != "" ? 1 : 0

  name  = "/${var.project_name}/COHERE_API_KEY"
  type  = "SecureString"
  value = var.cohere_api_key
}
