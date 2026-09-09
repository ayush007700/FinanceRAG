/*
 * State backend bootstrap.
 *
 * Chicken-and-egg: the main stack keeps its state in an S3 bucket, but a bucket
 * declared inside that stack cannot hold the state describing itself. So this
 * is a separate root module with *local* state, applied once, that creates only
 * the bucket.
 *
 * Its own state file is disposable on purpose. Everything here is either
 * `prevent_destroy` or trivially re-importable, so losing bootstrap state costs
 * an import, not an outage -- which is why it is acceptable for this one module
 * to keep state on a laptop while the stack that matters does not.
 *
 *   cd infra/terraform/bootstrap
 *   terraform init && terraform apply
 *
 * Then uncomment the backend block in ../versions.tf and run, from ../:
 *
 *   terraform init -migrate-state
 *
 * Locking uses S3 conditional writes (`use_lockfile`), so there is no DynamoDB
 * table to provision or pay for. That requires Terraform >= 1.10.
 */

terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
      App       = "FinanceRAG"
      Component = "tfstate"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "ap-south-1"
}

variable "project_name" {
  type    = string
  default = "source-advisors-finance-rag"
}

resource "aws_s3_bucket" "tfstate" {
  bucket = "${var.project_name}-tfstate"

  # The one resource in this repo that must outlive a careless `destroy`:
  # deleting it discards the record of everything else that exists.
  lifecycle {
    prevent_destroy = true
  }
}

# Versioning is what makes a corrupted or truncated state recoverable. Without
# it, a failed apply that writes a partial state leaves no earlier copy to roll
# back to, and the stack has to be reconstructed by import.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

# State holds every value marked `sensitive` in plaintext -- the RDS password
# and the CD user's secret access key among them. Encryption at rest is the
# minimum; the access policy below is what actually keeps it private.
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Old versions accumulate on every apply. Ninety days is long enough to recover
# from a mistake noticed late, short enough that the bucket does not grow
# without bound.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# HTTPS only. State in flight carries the same secrets as state at rest.
resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.tfstate.arn,
        "${aws_s3_bucket.tfstate.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}

output "state_bucket" {
  description = "Put this in the backend block in ../versions.tf"
  value       = aws_s3_bucket.tfstate.bucket
}
