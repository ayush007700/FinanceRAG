# RDS Postgres with pgvector -- replaces the external Neo4j Aura dependency.
# Lives in the private subnets; only ECS tasks can reach it.

resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-db-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${var.project_name}-db-subnets" }
}

resource "aws_security_group" "rds" {
  name        = "${var.project_name}-rds-sg"
  description = "Postgres reachable only from ECS tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from ECS tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# pgvector ships in the standard Postgres engine as an available extension from
# 15.2 onward, so no custom parameter group is needed -- the migration issues
# CREATE EXTENSION vector.
resource "aws_db_instance" "main" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period = var.db_backup_retention_days
  skip_final_snapshot     = var.db_skip_final_snapshot
  final_snapshot_identifier = (
    var.db_skip_final_snapshot ? null : "${var.project_name}-final-${formatdate("YYYYMMDDhhmmss", timestamp())}"
  )
  deletion_protection = var.db_deletion_protection

  auto_minor_version_upgrade   = true
  performance_insights_enabled = false # keeps the lean footprint cheap

  tags = { Name = "${var.project_name}-db" }

  lifecycle {
    # engine_version drifts as AWS applies minor upgrades; with a major-only
    # version that is expected, not a change to reconcile.
    ignore_changes = [final_snapshot_identifier, engine_version]
  }
}

locals {
  database_url = format(
    "postgresql+psycopg://%s:%s@%s:%s/%s",
    var.db_username,
    urlencode(var.db_password),
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    var.db_name,
  )
}
