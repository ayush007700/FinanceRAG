terraform {
  # 1.10 is the floor for S3 native state locking (use_lockfile), which is what
  # removes the need for a DynamoDB lock table.
  required_version = ">= 1.10.0"

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

  # State is remote so the stack is not owned by one laptop: locking stops two
  # applies from interleaving, versioning makes a corrupted state recoverable,
  # and history survives losing the machine.
  #
  # The bucket is created by ./bootstrap, a separate root module with local
  # state -- a bucket declared in this stack cannot hold the state describing
  # itself. Run that once before `terraform init -migrate-state` here.
  #
  # use_lockfile is S3 conditional-write locking, which replaces the DynamoDB
  # table the older pattern needed.
  backend "s3" {
    bucket       = "source-advisors-finance-rag-tfstate"
    key          = "finance-rag/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
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
