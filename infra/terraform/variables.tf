variable "aws_region" {
  type        = string
  description = "AWS region for all resources"
  default     = "ap-south-1"
}

variable "project_name" {
  type        = string
  description = "Name prefix for resources"
  default     = "source-advisors-finance-rag"
}

variable "container_image" {
  type        = string
  description = "ECR image URI including tag, e.g. 123.dkr.ecr.ap-south-1.amazonaws.com/finance-rag:sha"
  default     = ""
}

variable "openai_api_key" {
  type        = string
  description = "OpenAI API key stored in SSM Parameter Store"
  sensitive   = true
}

variable "neo4j_uri" {
  type        = string
  description = "Neo4j Aura URI, e.g. neo4j+s://xxxx.databases.neo4j.io"
}

variable "neo4j_user" {
  type        = string
  description = "Neo4j username (Aura default is often neo4j)"
  default     = "neo4j"
}

variable "neo4j_password" {
  type        = string
  description = "Neo4j Aura password stored in SSM"
  sensitive   = true
}

variable "neo4j_database" {
  type        = string
  description = "Neo4j database name"
  default     = "neo4j"
}

variable "desired_count" {
  type        = number
  description = "Starting number of ECS tasks"
  default     = 2
}

variable "min_capacity" {
  type        = number
  default     = 1
}

variable "max_capacity" {
  type        = number
  default     = 6
}

variable "cpu" {
  type        = number
  description = "Fargate CPU units (1024 = 1 vCPU)"
  default     = 1024
}

variable "memory" {
  type        = number
  description = "Fargate memory in MiB"
  default     = 2048
}

variable "github_org_repo" {
  type        = string
  description = "GitHub repo allowed to assume deploy role via OIDC, format owner/repo"
  default     = ""
}
