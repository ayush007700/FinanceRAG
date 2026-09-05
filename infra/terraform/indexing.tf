# Indexing runs as its own task, sized for its own workload.
#
# The API service is 0.5 vCPU / 1 GiB because serving requests is IO-bound on
# model APIs. Parsing is not: pdfplumber holds the full page model for a
# 113-page publication, and indexing this corpus inside the API container was
# SIGKILLed with exit 137 every time. Same image, different shape.

resource "aws_ecs_task_definition" "index" {
  family                   = "${var.project_name}-index"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.index_task_cpu
  memory                   = var.index_task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "index"
      image     = local.image
      essential = true
      # Overridden per run with the job id; this default makes the task
      # runnable by hand for a full re-index.
      command = ["finance-rag", "index", "data/corpus"]
      environment = [
        { name = "APP_ENV", value = "production" },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "S3_BUCKET", value = aws_s3_bucket.uploads.bucket },
        { name = "OPENAI_CHAT_MODEL", value = "gpt-4o" },
        { name = "OPENAI_EMBEDDING_MODEL", value = "text-embedding-3-large" },
        { name = "OPENAI_EMBEDDING_DIMENSIONS", value = "1536" },
        { name = "MULTIMODAL_ENABLED", value = "true" },
        # Indexing writes; it never serves a query, so caching is pointless here.
        { name = "CACHE_ENABLED", value = "false" }
      ]
      secrets = concat(
        [
          {
            name      = "OPENAI_API_KEY"
            valueFrom = aws_ssm_parameter.openai_api_key.arn
          },
          {
            name      = "DATABASE_URL"
            valueFrom = aws_ssm_parameter.database_url.arn
          }
        ],
        var.cohere_api_key != "" ? [{
          name      = "COHERE_API_KEY"
          valueFrom = aws_ssm_parameter.cohere_api_key[0].arn
        }] : []
      )
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "index"
        }
      }
    }
  ])
}

# The API launches indexing tasks, so its task role needs RunTask and the right
# to pass the roles the launched task assumes.
resource "aws_iam_role_policy" "task_run_index" {
  name = "${var.project_name}-task-run-index"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask", "ecs:DescribeTasks", "ecs:ListTasks"]
        Resource = "*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = aws_ecs_cluster.this.arn
          }
        }
      },
      {
        Effect = "Allow"
        # Without PassRole the launched task cannot assume the roles that let it
        # read secrets and reach S3, and RunTask is refused.
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.ecs_task_execution.arn,
          aws_iam_role.ecs_task.arn
        ]
      }
    ]
  })
}

output "index_task_family" {
  description = "Task definition the API launches for indexing"
  value       = aws_ecs_task_definition.index.family
}
