# GitHub Actions can deploy without long-lived AWS access keys (OIDC)

data "aws_caller_identity" "current" {}

locals {
  # Accept either "owner/repo" or "https://github.com/owner/repo"
  github_repo = trimspace(
    trimsuffix(
      replace(replace(var.github_org_repo, "https://github.com/", ""), "http://github.com/", ""),
      ".git"
    )
  )
  github_oidc_enabled = local.github_repo != "" && can(regex("^[^/]+/[^/]+$", local.github_repo))
}

resource "aws_iam_openid_connect_provider" "github" {
  count = local.github_oidc_enabled ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4c4df9203ae",
  ]
}

resource "aws_iam_role" "github_actions" {
  count = local.github_oidc_enabled ? 1 : 0
  name  = "${var.project_name}-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github[0].arn
      }
      # Include TagSession — required by newer configure-aws-credentials
      Action = [
        "sts:AssumeRoleWithWebIdentity",
        "sts:TagSession"
      ]
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Must be owner/repo — NOT a https://github.com/... URL
          "token.actions.githubusercontent.com:sub" = "repo:${local.github_repo}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions" {
  count = local.github_oidc_enabled ? 1 : 0
  name  = "${var.project_name}-gha-deploy"
  role  = aws_iam_role.github_actions[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
          "ecr:DescribeRepositories",
          "ecr:DescribeImages"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:DescribeServices",
          "ecs:DescribeTaskDefinition",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:RegisterTaskDefinition",
          "ecs:UpdateService"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.ecs_task_execution.arn,
          aws_iam_role.ecs_task.arn
        ]
      }
    ]
  })
}
