# Source Advisors FinanceRAG

Advanced Retrieval-Augmented Generation for **Source Advisors** — a USA & UK specialized tax consulting firm — built with **LangGraph**, **Postgres/pgvector**, and **OpenAI**.

Relationships, trust, integrity, and hard work guide how this system answers questions about R&D Tax Credits, Cost Segregation, Energy Efficiency (§179D & §45L), Sales & Use Tax, ITC/PTC, Commercial Property Tax, and LIFO inventory solutions.

## Architecture

```text
Documents (MD/PDF/JSON)
        │
        ▼
 Ingestion → Hierarchical Chunking → OpenAI Embeddings
        │
        ▼
 Postgres + pgvector (documents / chunks / entities / chunk_entities)
   • HNSW vector index   • tsvector GIN index   • entity adjacency
        │
        ▼
 Hybrid Retrieval — Reciprocal Rank Fusion over dense + fulltext + entity expansion
                    (+ parent-section context)
        │
        ▼
 Rerank (Cohere rerank-v3.5 cross-encoder; falls back to RRF order)
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
- Docker (for Postgres + Redis)
- OpenAI API key

### 2. Configure

```bash
cp .env.example .env
# set OPENAI_API_KEY
```

### 3. Start Postgres + install

```bash
docker compose up -d postgres redis
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
alembic upgrade head          # creates the pgvector schema
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
3. **retrieve** — RRF over pgvector dense + Postgres fulltext + entity expansion, plus parent sections  
4. **generate** — grounded OpenAI answer with `[chunk_id]` citations  
5. **output_guardrails** — disclaimer, citation policy, overconfidence checks + metrics

## Retrieval: Reciprocal Rank Fusion

Dense and sparse results are fused by rank, not by score:

    RRF(d) = sum over rankers r of  w_r / (k + rank_r(d))       # k = 60

`ts_rank_cd` is unbounded and cosine lives in [-1, 1], so a weighted score blend
would be adding incomparable quantities. Min-max normalising first is worse: it
pins the best candidate to 1.0 whatever its real quality, which makes a failed
retrieval indistinguishable from a perfect one.

Dense + sparse fusion runs in a single SQL statement
(`PgVectorStore.hybrid_search_rrf`); entity expansion is folded in as a
down-weighted third ranker.

Because RRF scores are ordinal — top-1 is always ~1/(k+1) — they carry no
absolute meaning. Abstention therefore reads `RetrievedChunk.cosine`, the raw
similarity the store returns alongside the fused score.

## Observability

Two tracers, deliberately not redundant:

| | where | use |
|---|---|---|
| **LangSmith** | SaaS | hosted convenience for development |
| **Langfuse** | self-hosted, in-VPC | the one that can point at production |

Tax queries and retrieved passages leave the deployment with a SaaS tracer,
which for this content needs sign-off. Langfuse runs beside the app instead.

```bash
docker compose --profile langfuse up -d     # UI on http://localhost:3001
```

It is behind a compose profile because it is four extra containers (Postgres,
ClickHouse, MinIO, plus the app) -- the default dev stack should not pay for
them. Create a project in the UI, put its keys in `.env`, set
`LANGFUSE_ENABLED=true`.

Each agent node becomes its own span, so a slow or looping request is readable
by role rather than as one opaque call. Evaluation runs publish their metrics as
Langfuse scores; `eval_runs` in Postgres remains the system of record.

Every path fails open: a missing key, an unreachable host or a broken handler
degrades to no tracing, never to no answers.

## Evaluation

```bash
python scripts/run_eval.py                 # full run (spends model credits)
python scripts/run_eval.py --no-judge      # retrieval metrics only, cheaper
python scripts/run_eval.py --save-baseline # pin current numbers as the baseline

python scripts/run_eval.py --list-runs     # run history (no model calls)
python scripts/run_eval.py --diff 2 3      # compare two runs
```

Every run is persisted to `eval_runs` / `eval_cases` with the git SHA and a
snapshot of the settings that shaped it -- a metric without its configuration is
a number, not a measurement. `--diff` reports metric deltas, configuration
changes, **and the individual cases that flipped**, which is the part that leads
to a fix: an average that moved says go looking, a named case that changed says
where. Also exposed at `GET /v1/eval/runs`.

Reports land in `data/processed/eval_report.json`; the `Eval` workflow runs the
same harness on demand and weekly.

Metrics are split by what is actually measurable:

| | reported | why |
|---|---|---|
| **Online** (per request) | latency, num_retrieved, top/mean/min cosine, rerank score, citation grounding | no relevance labels exist at request time |
| **Offline** (golden set) | hit rate, MRR, nDCG, precision@k, recall@k, faithfulness, abstention accuracy | labels available |

`hit_rate`, `mrr` and `ndcg` are `None` online. That is deliberate: computing
them from the retriever's own scores makes them constants, not measurements.

The golden set (`data/eval/golden_set.json`) labels documents by **source
basename or corpus doc_id**, never by generated doc_id -- those are hashes of
the absolute path and differ per machine. Six cases are unanswerable from the
corpus and assert that the system *refuses*; a ranking metric cannot catch a
confident answer to a question the corpus does not support.

Unit tests (no live Postgres/OpenAI required for core tests; store integration tests skip without a database):

```bash
pytest -q
```

## Documentation

| | |
|---|---|
| [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) | Architecture, RRF, the six agents, memory, evaluation, guardrails — and the war stories with real numbers |
| [`docs/AWS_AND_TERRAFORM.md`](docs/AWS_AND_TERRAFORM.md) | Services and why each was chosen, the NAT cost trade, Terraform patterns, CI/CD bootstrap order, UI hosting |

## AWS deployment (RDS Postgres + ECS + GitHub Actions)

Full beginner guide: [`docs/TERRAFORM_AND_CICD.md`](docs/TERRAFORM_AND_CICD.md)

Enterprise features (Redis semantic cache, LangSmith, multimodal): [`docs/ENTERPRISE_FEATURES.md`](docs/ENTERPRISE_FEATURES.md)

Next.js dashboard: [`web/`](web/) — run `npm run dev` in `web/` (API on `:8000`, UI on `:3000`).

Flow:

```text
GitHub Actions CI/CD → ECR image → ECS Fargate → RDS Postgres (pgvector)
                         │
                         ├── OpenAI API
                         └── CloudWatch (+ local Prometheus/Grafana)
```

1. Point local `.env` at your database (`DATABASE_URL=...`), run `alembic upgrade head`, then `finance-rag index data/corpus`
2. Copy `infra/terraform/terraform.tfvars.example` → `terraform.tfvars` (`db_password` + OpenAI + `github_org_repo`)
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
