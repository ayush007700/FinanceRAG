# Source Advisors FinanceRAG

Advanced Retrieval-Augmented Generation for **Source Advisors** — a USA & UK specialized tax consulting firm — built with **LangGraph**, **Neo4j**, and **OpenAI**.

Relationships, trust, integrity, and hard work guide how this system answers questions about R&D Tax Credits, Cost Segregation, Energy Efficiency (§179D & §45L), Sales & Use Tax, ITC/PTC, Commercial Property Tax, and LIFO inventory solutions.

## Architecture

```text
Documents (MD/PDF/JSON)
        │
        ▼
 Ingestion → Hierarchical Chunking → OpenAI Embeddings
        │
        ▼
 Neo4j (Document / Chunk / ServiceLine / Entity + Vector + Fulltext)
        │
        ▼
 Hybrid Retrieval (dense + BM25/fulltext + graph expand + parent context)
        │
        ▼
 Rerank (Cohere or OpenAI judge)
        │
        ▼
 LangGraph Agent (guardrails → rewrite → retrieve → generate → output guardrails)
        │
        ▼
 FastAPI + Prometheus + CloudWatch (ECS/Fargate)
```

### Capability map

| Area | Implementation |
|------|----------------|
| Data ingestion | `finance_rag.ingestion` — MD/TXT/PDF/JSON with service-line & jurisdiction inference |
| Better chunking | Structure-aware + hierarchical parent/child token windows |
| Better embeddings | `text-embedding-3-large` with configurable dimensions |
| Better ranking | Hybrid fusion (α·dense + (1-α)·sparse) + graph boost + reranker |
| Retrieval metrics | Hit rate, MRR, nDCG, context precision/recall, LLM faithfulness/relevance |
| Generation | OpenAI chat (`gpt-4o` by default) via LangGraph |
| Guardrails | Injection/PII filters, citation checks, low-confidence refusal, tax disclaimer |
| AWS deploy | Docker → ECR, Terraform ECS Fargate task, CW dashboard + latency alarm |
| Monitoring | Structured JSON logs, Prometheus `/metrics`, CloudWatch custom metrics |

## Quick start

### 1. Prerequisites

- Python 3.11+
- Docker (for Neo4j)
- OpenAI API key

### 2. Configure

```bash
cp .env.example .env
# set OPENAI_API_KEY
```

### 3. Start Neo4j + API

```bash
docker compose up -d neo4j
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 4. Index Source Advisors corpus

```bash
finance-rag index data/corpus
```

### 5. Ask

```bash
finance-rag ask "How does Source Advisors support CPA firms on R&D tax credits?"
```

Or run the API:

```bash
uvicorn finance_rag.api.app:app --reload --port 8000
```

```bash
curl -X POST http://localhost:8000/v1/ask ^
  -H "Content-Type: application/json" ^
  -d "{\"query\":\"What is the four-part test for R&D credits?\"}"
```

## LangGraph flow

1. **input_guardrails** — block injection, redact PII  
2. **rewrite_query** — expand tax acronyms for retrieval  
3. **retrieve** — hybrid Neo4j vector + fulltext + entity expansion + parent sections  
4. **generate** — grounded OpenAI answer with `[chunk_id]` citations  
5. **output_guardrails** — disclaimer, citation policy, overconfidence checks + metrics

## Evaluation

```bash
python scripts/run_eval.py
```

Reports land in `data/processed/eval_report.json`.

Unit tests (no live Neo4j/OpenAI required for core tests):

```bash
pytest -q
```

## AWS deployment (Neo4j Aura + ECS + GitHub Actions)

Full beginner guide: [`docs/TERRAFORM_AND_CICD.md`](docs/TERRAFORM_AND_CICD.md)

Flow:

```text
GitHub Actions CI/CD → ECR image → ECS Fargate → Neo4j Aura
                         │
                         ├── OpenAI API
                         └── CloudWatch (+ local Prometheus/Grafana)
```

1. Point local `.env` at Aura (`neo4j+s://...`) and run `finance-rag index data/corpus`
2. Copy `infra/terraform/terraform.tfvars.example` → `terraform.tfvars` (Aura + OpenAI + `github_org_repo`)
3. `terraform init && terraform apply`
4. Add GitHub secret `AWS_ROLE_ARN` from Terraform output
5. Push to `main` → CD builds/pushes ECR and rolls ECS behind the ALB

Includes **ALB**, **CPU/request autoscaling**, **SSM secrets**, and **CloudWatch** dashboards/alarms.

## Project layout

```text
src/finance_rag/          # application
.github/workflows/        # CI (pytest) + CD (ECR/ECS)
infra/terraform/          # VPC, ALB, ECS, secrets, autoscaling, CloudWatch
docs/TERRAFORM_AND_CICD.md
data/corpus/              # sample Source Advisors knowledge
```

## Responsible use

Outputs are **decision-support** for Source Advisors teams and partner CPA firms — not formal tax or legal advice. Always have qualified professionals review client deliverables.
