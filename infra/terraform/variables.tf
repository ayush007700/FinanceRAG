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

variable "cohere_api_key" {
  type        = string
  description = "Cohere API key for the rerank cross-encoder. Leave empty to skip reranking (the app falls back to RRF order)."
  sensitive   = true
  default     = ""
}

variable "auth_api_keys" {
  type        = string
  sensitive   = true
  description = <<-EOT
    Bearer credentials for the /v1 API, comma-separated as
    "key_id:org_id:scopes:secret". Scopes are '|'-separated from ask/index/read,
    or '*'. The org is a property of the key, so a caller cannot name another
    tenant.

    Empty is not a way to leave the API open: the task refuses to start when
    auth is on and no keys are configured. Set auth_enabled = false for that,
    deliberately.
  EOT
  default     = ""
}

variable "auth_enabled" {
  type        = bool
  description = "Require a bearer credential on /v1. Turn off only when a gateway in front of the ALB authenticates instead."
  default     = true
}

variable "cohere_rerank_model" {
  type        = string
  description = "Cohere rerank model id"
  default     = "rerank-v3.5"
}

variable "db_name" {
  type        = string
  description = "Postgres database name"
  default     = "finrag"
}

variable "db_username" {
  type        = string
  description = "Postgres master username"
  default     = "finrag"
}

variable "db_password" {
  type        = string
  description = "Postgres master password (stored in SSM as part of DATABASE_URL)"
  sensitive   = true
}

variable "db_instance_class" {
  type        = string
  description = "RDS instance class. db.t4g.micro is the lean default."
  default     = "db.t4g.micro"
}

variable "db_engine_version" {
  type        = string
  description = <<-EOT
    Postgres version. Major-only ("16") is deliberate: AWS then selects the
    latest available minor, and minor availability differs by region. Pinning
    "16.4" failed in ap-south-1, which offers 16.10 and later but never 16.4.
  EOT
  default     = "16"
}

variable "db_allocated_storage" {
  type        = number
  description = "Initial storage in GiB"
  default     = 20
}

variable "db_max_allocated_storage" {
  type        = number
  description = "Storage autoscaling ceiling in GiB"
  default     = 100
}

variable "db_multi_az" {
  type        = bool
  description = "Multi-AZ standby. Off by default to keep demo cost low."
  default     = false
}

variable "db_backup_retention_days" {
  type    = number
  default = 7
}

variable "db_skip_final_snapshot" {
  type        = bool
  description = "Skip the final snapshot on destroy (fine for demo stacks)"
  default     = true
}

variable "db_deletion_protection" {
  type    = bool
  default = false
}

variable "enable_nat_gateway" {
  type        = bool
  description = <<-EOT
    Run ECS tasks in private subnets behind a NAT gateway.

    false places tasks in public subnets with no inbound path except the ALB
    security group, removing the largest single cost in the stack. The tasks
    then carry public IPs, so the security group is the only boundary -- keep
    this true where an obligation requires no public interface.
  EOT
  default     = false
}

variable "acm_certificate_arn" {
  type        = string
  description = "ACM certificate for HTTPS. Empty leaves the listener on HTTP only."
  default     = ""
}

variable "alarm_email" {
  type        = string
  description = "Address subscribed to CloudWatch alarms. Empty means alarms fire nowhere."
  default     = ""
}

variable "s3_force_destroy" {
  type        = bool
  description = "Allow terraform destroy to delete a non-empty uploads bucket"
  default     = false
}

variable "index_task_cpu" {
  type        = number
  description = "Fargate CPU for the indexing task. Parsing is CPU-bound, unlike request serving."
  default     = 2048
}

variable "index_task_memory" {
  type        = number
  description = <<-EOT
    Fargate memory (MiB) for the indexing task. pdfplumber holds the full page
    model for a large publication; this corpus was OOM-killed at 1024 and
    completed at 8192.
  EOT
  default     = 8192
}

variable "desired_count" {
  type        = number
  description = "Starting number of ECS tasks. Autoscaling raises this under load."
  default     = 1
}

variable "min_capacity" {
  type    = number
  default = 1
}

variable "max_capacity" {
  type    = number
  default = 6
}

variable "cpu" {
  type        = number
  description = "Fargate CPU units (1024 = 1 vCPU). The work is IO-bound on model APIs, not CPU-bound."
  default     = 512
}

variable "memory" {
  type        = number
  description = "Fargate memory in MiB"
  default     = 1024
}

variable "github_org_repo" {
  type        = string
  description = "GitHub repo as owner/name (e.g. ayush007700/FinanceRAG). Do NOT include https://github.com/"
  default     = ""
}
