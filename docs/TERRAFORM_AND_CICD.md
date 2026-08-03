# Terraform + CI/CD Guide (FinanceRAG → Neo4j Aura → ECS)

This document explains **why Terraform exists**, what each file does, and how
**GitHub Actions → ECR → ECS Fargate → Neo4j Aura** works, including ALB,
autoscaling, secrets, and CloudWatch.

## Why Terraform is needed

Terraform is **Infrastructure as Code (IaC)**.

Without it you would click around the AWS console to create a VPC, load balancer,
ECS cluster, IAM roles, secrets, alarms… and nobody could reproduce or review it.

With Terraform you:

1. **Declare** the desired AWS resources in `.tf` files
2. Run `terraform plan` to preview changes
3. Run `terraform apply` to create/update them safely

Think of it like this:

| App code | Infra code |
|---|---|
| Python creates the RAG API | Terraform creates the AWS home the API lives in |
| `git` tracks app changes | Terraform state tracks AWS resource IDs |

You still need Terraform **even with CI/CD**:
- CI/CD deploys **new Docker images** to ECS
- Terraform creates/updates the **platform** (network, ALB, secrets, autoscaling)

---

## Mental model of the production flow

```text
Developer pushes to main
        │
        ▼
GitHub Actions CI  → pytest
GitHub Actions CD  → docker build → push ECR → update ECS service
        │
        ▼
ALB (public HTTP)  →  ECS Fargate tasks (private subnets)
        │                      │
        │                      ├── OpenAI API (outbound via NAT)
        │                      ├── Neo4j Aura (outbound via NAT)
        │                      └── CloudWatch Logs + Metrics
        ▼
Users hit http://<alb-dns>/v1/ask
```

Prometheus/Grafana remain great for **local/dev**. In AWS, start with
**CloudWatch** (already wired). You can later scrape ECS `/metrics` into
Amazon Managed Prometheus + Grafana if needed.

---

## Terraform files explained (beginner)

All under `infra/terraform/`:

| File | What it creates | Why it matters |
|---|---|---|
| `versions.tf` | Terraform + AWS provider versions | Pins tooling so teammates get same behavior |
| `variables.tf` | Input knobs (region, Aura URI, keys…) | Makes the stack reusable without editing code |
| `network.tf` | VPC, public/private subnets, IGW, NAT | Private ECS + public ALB; outbound to Aura/OpenAI |
| `security.tf` | Security groups (firewalls) | ALB accepts :80; ECS only accepts :8000 from ALB |
| `alb.tf` | Application Load Balancer + target group | Public entrypoint + health checks on `/health` |
| `ecr.tf` | Elastic Container Registry | Stores Docker images from GitHub Actions |
| `secrets.tf` | SSM Parameter Store SecureStrings | Injects OpenAI/Aura secrets at runtime (not in image) |
| `ecs.tf` | Cluster, task definition, service, IAM | Runs your container on Fargate |
| `autoscaling.tf` | App Auto Scaling policies | Scales tasks on CPU and ALB request rate |
| `monitoring.tf` | CloudWatch dashboard + alarms | Ops visibility and paging signals |
| `github_oidc.tf` | IAM role for GitHub Actions | Deploy without long-lived AWS access keys |
| `outputs.tf` | Printed values after apply | ALB URL, ECR URL, role ARN for GitHub |
| `terraform.tfvars.example` | Sample inputs | Copy to `terraform.tfvars` (gitignored) |

### Important AWS pieces in plain English

- **ALB**: internet-facing door. Routes `http://alb/...` to healthy containers.
- **Autoscaling**: if CPU or requests/target rise, ECS starts more Fargate tasks.
- **Secrets (SSM)**: `OPENAI_API_KEY`, `NEO4J_PASSWORD`, `NEO4J_URI` injected as env vars.
- **CloudWatch**: container logs under `/ecs/<project>` + dashboards/alarms.

---

## Neo4j Aura setup

1. In Aura console, create/open your instance.
2. Copy **Connection URI** — usually:
   `neo4j+s://xxxxxxxx.databases.neo4j.io`
3. Note username (often `neo4j`) and password.
4. Put them in:
   - local `.env` for indexing/dev
   - `infra/terraform/terraform.tfvars` for AWS

Local index against Aura:

```powershell
# .env
NEO4J_URI=neo4j+s://xxxxxxxx.databases.neo4j.io
NEO4J_USER=neo4j
NEO4J_PASSWORD=...
NEO4J_DATABASE=neo4j

finance-rag index data/corpus
```

Aura is reachable from your laptop and from ECS (via NAT). You do **not** run Neo4j in ECS.

---

## One-time bootstrap (you run this once)

### 1. Prerequisites
- AWS account + CLI configured (`aws configure`)
- Terraform >= 1.5 installed
- GitHub repo for this project
- Neo4j Aura instance
- OpenAI key

### 2. Create tfvars

```powershell
cd infra/terraform
copy terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars with Aura URI/password, OpenAI key, github_org_repo
```

Set `github_org_repo = "YOUR_USER/FinanceRAG"` (exact GitHub `owner/repo`).

### 3. Apply infrastructure

```powershell
terraform init
terraform plan
terraform apply
```

Save outputs:
- `api_base_url`
- `ecr_repository_url`
- `github_actions_role_arn`
- `ecs_cluster_name` / `ecs_service_name`

### 4. GitHub configuration

**Secret**
- `AWS_ROLE_ARN` = output `github_actions_role_arn`

**Variables** (optional overrides)
- `AWS_REGION`
- `ECS_CLUSTER`
- `ECS_SERVICE`
- `ECS_TASK_FAMILY`

### 5. First image deploy

Push to `main` (or run workflow **CD — Build ECR & Deploy ECS** manually).

Then verify:

```text
http://<alb_dns_name>/health
http://<alb_dns_name>/v1/ask
```

### 6. CloudWatch

AWS Console → CloudWatch → Dashboards → `source-advisors-finance-rag-dashboard`  
Logs → Log groups → `/ecs/source-advisors-finance-rag`

---

## CI vs CD in this repo

| Workflow | File | When | Does |
|---|---|---|---|
| CI | `.github/workflows/ci.yml` | PR / push | Install + pytest |
| CD | `.github/workflows/cd.yml` | push to main | Build image → ECR → rolling ECS deploy |

CD does **not** recreate VPC/ALB. It only ships new application code.

---

## Cost note

This stack includes a **NAT Gateway** (for private ECS outbound). That is the main ongoing cost besides Fargate. For cheaper experiments you can later simplify to public-subnet tasks (less secure).

---

## Troubleshooting

| Symptom | Check |
|---|---|
| ECS tasks crash looping | CloudWatch logs; wrong Aura URI/password; missing OpenAI key |
| ALB 502/503 | Target group health `/health`; security groups; desired tasks = 0 |
| GitHub CD cannot assume role | `AWS_ROLE_ARN`, `github_org_repo`, OIDC provider |
| Aura connection timeout | ECS egress + NAT; URI must be `neo4j+s://...` |
