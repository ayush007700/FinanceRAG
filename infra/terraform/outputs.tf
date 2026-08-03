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
  description = "Set this as GitHub secret AWS_ROLE_ARN when github_org_repo is set"
  value       = try(aws_iam_role.github_actions[0].arn, null)
}
