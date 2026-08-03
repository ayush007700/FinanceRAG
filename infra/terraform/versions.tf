terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Optional later: store state in S3 instead of local disk
  # backend "s3" {
  #   bucket = "your-tf-state-bucket"
  #   key    = "finance-rag/terraform.tfstate"
  #   region = "us-east-1"
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
