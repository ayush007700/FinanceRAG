output "alb_dns_name" {
  description = "Public URL hostname for the API (http://<this>/health)"
  value       = aws_lb.api.dns_name
}

output "api_base_url" {
  value = "http://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "ecs_service_name" {
  value = aws_ecs_service.api.name
}

output "task_definition_family" {
  value = aws_ecs_task_definition.api.family
}

output "cloudwatch_log_group" {
  value = aws_cloudwatch_log_group.api.name
}

output "cloudwatch_dashboard" {
  value = aws_cloudwatch_dashboard.rag.dashboard_name
}

output "github_actions_role_arn" {
  description = "OIDC role ARN (optional). Prefer access keys below if OIDC keeps failing."
  value       = try(aws_iam_role.github_actions[0].arn, null)
}

output "github_actions_access_key_id" {
  description = "Put this in GitHub secret AWS_ACCESS_KEY_ID"
  value       = aws_iam_access_key.github_actions.id
}

output "github_actions_secret_access_key" {
  description = "Put this in GitHub secret AWS_SECRET_ACCESS_KEY"
  value       = aws_iam_access_key.github_actions.secret
  sensitive   = true
}

output "rds_endpoint" {
  description = "Postgres endpoint (private; reachable only from ECS tasks)"
  value       = aws_db_instance.main.address
}

output "database_ssm_parameter" {
  description = "SSM parameter holding the full DATABASE_URL"
  value       = aws_ssm_parameter.database_url.name
}

output "uploads_bucket" {
  description = "S3 bucket for uploaded documents"
  value       = aws_s3_bucket.uploads.bucket
}

output "alarm_topic_arn" {
  description = "SNS topic the CloudWatch alarms publish to"
  value       = aws_sns_topic.alarms.arn
}

output "api_url" {
  description = "Base URL. HTTPS once acm_certificate_arn is set."
  value       = var.acm_certificate_arn == "" ? "http://${aws_lb.api.dns_name}" : "https://${aws_lb.api.dns_name}"
}

output "nat_gateway_enabled" {
  description = "False means tasks run in public subnets, SG-locked, without a NAT gateway."
  value       = var.enable_nat_gateway
}

output "migrate_task_family" {
  description = "One-shot migration task definition, run by CD before deploying"
  value       = aws_ecs_task_definition.migrate.family
}

output "private_subnet_ids" {
  description = "Subnets the migration task runs in"
  value       = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "ecs_security_group_id" {
  value = aws_security_group.ecs.id
}
