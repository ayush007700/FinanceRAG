# ECS Fargate runs your FastAPI container without managing EC2 servers

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "this" {
  name = "${var.project_name}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name = "${var.project_name}-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow the execution role to read SSM secrets into the container
resource "aws_iam_role_policy" "ecs_exec_ssm" {
  name = "${var.project_name}-exec-ssm"
  role = aws_iam_role.ecs_task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssm:GetParameters",
        "ssm:GetParameter"
      ]
      Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project_name}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
    }]
  })
}

resource "aws_iam_role" "ecs_task" {
  name = "${var.project_name}-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_cloudwatch" {
  name = "${var.project_name}-cw"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "cloudwatch:PutMetricData",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ]
      Resource = "*"
    }]
  })
}

locals {
  # Prefer explicit image from tfvars/CI; otherwise use ECR repo :latest placeholder
  image = var.container_image != "" ? var.container_image : "${aws_ecr_repository.api.repository_url}:latest"
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project_name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.image
      essential = true
      portMappings = [{
        containerPort = 8000
        protocol      = "tcp"
      }]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "AWS_CLOUDWATCH_NAMESPACE", value = "FinanceRAG/SourceAdvisors" },
        { name = "ENABLE_PROMETHEUS", value = "true" },
        { name = "OPENAI_CHAT_MODEL", value = "gpt-4o" },
        { name = "OPENAI_EMBEDDING_MODEL", value = "text-embedding-3-large" },
        { name = "OPENAI_EMBEDDING_DIMENSIONS", value = "1536" },
        { name = "S3_BUCKET", value = aws_s3_bucket.uploads.bucket },
        { name = "ENFORCE_TENANCY", value = "true" },
        # Pinned to the CloudFront origin. A wildcard here would be rejected
        # by browsers as soon as credentials are involved.
        { name = "CORS_ORIGINS", value = "https://${aws_cloudfront_distribution.ui.domain_name}" },
        { name = "APP_ENV", value = "production" },
        { name = "RERANK_PROVIDER", value = var.cohere_api_key != "" ? "cohere" : "none" },
        { name = "COHERE_RERANK_MODEL", value = var.cohere_rerank_model },
        { name = "CACHE_ENABLED", value = "false" },
        { name = "MULTIMODAL_ENABLED", value = "true" },
        { name = "LANGSMITH_TRACING", value = "false" },
        { name = "LANGSMITH_PROJECT", value = "source-advisors-finance-rag" }
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
          awslogs-stream-prefix = "api"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 40
      }
    }
  ])
}

# One-shot migration task.
#
# GitHub Actions cannot reach RDS: the database sits in private subnets and is
# not publicly accessible, which is the point. So migrations run *inside* the
# VPC as a task using the same image, and the deploy waits for it to exit 0.
#
# Deliberately not run from the API container on startup: with more than one
# task that races N concurrent migration runs against the same schema.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.project_name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "migrate"
      image     = local.image
      essential = true
      command   = ["alembic", "upgrade", "head"]
      environment = [
        { name = "APP_ENV", value = "production" },
        { name = "AWS_REGION", value = var.aws_region }
      ]
      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = aws_ssm_parameter.database_url.arn
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "migrate"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "api" {
  name            = "${var.project_name}-svc"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    # Without a NAT gateway the tasks must sit in public subnets to reach
    # OpenAI and Cohere at all. Inbound is still only the ALB security group;
    # the public IP exists for egress, not for reachability.
    subnets          = var.enable_nat_gateway ? aws_subnet.private[*].id : aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = !var.enable_nat_gateway
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200

  depends_on = [aws_lb_listener.http]

  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }
}
