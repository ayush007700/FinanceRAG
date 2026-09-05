terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State lives on one laptop until this is enabled. That means no locking, no
  # history, and a stack that only one person can safely change. Create the
  # bucket once (versioned, encrypted, public access blocked), then uncomment.
  #
  # S3 native locking (use_lockfile) replaces the old DynamoDB table, so there
  # is no second resource to provision.
  #
  # backend "s3" {
  #   bucket       = "source-advisors-finance-rag-tfstate"
  #   key          = "finance-rag/terraform.tfstate"
  #   region       = "ap-south-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
      App       = "FinanceRAG"
    }
  }
}
