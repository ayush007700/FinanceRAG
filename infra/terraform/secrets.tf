# Secrets: never bake API keys into the Docker image.
# ECS injects them at runtime from AWS Systems Manager Parameter Store.

resource "aws_ssm_parameter" "openai_api_key" {
  name  = "/${var.project_name}/OPENAI_API_KEY"
  type  = "SecureString"
  value = var.openai_api_key
}

resource "aws_ssm_parameter" "neo4j_password" {
  name  = "/${var.project_name}/NEO4J_PASSWORD"
  type  = "SecureString"
  value = var.neo4j_password
}

resource "aws_ssm_parameter" "neo4j_uri" {
  name  = "/${var.project_name}/NEO4J_URI"
  type  = "String"
  value = var.neo4j_uri
}
