# Source Advisors FinanceRAG

Retrieval-augmented advisory for **Source Advisors** — a USA & UK specialised tax
consulting firm — built with **LangGraph**, **Postgres/pgvector** and **OpenAI**.

Covers R&D Tax Credits, Cost Segregation, Energy Efficiency (§179D & §45L),
Sales & Use Tax, ITC/PTC, Commercial Property Tax and LIFO inventory.

The design goal is narrow and specific: **a wrong answer must be harder to
produce than no answer**. Everything below follows from that.

---

## Architecture

```text
  PDF / MD / JSON
        │
        ▼
  layout-aware parsing ──▶ page-furniture stripping ──▶ hierarchical chunking
  (headings, tables kept atomic)                        (parent/child)
        │
        ▼
  embeddings (text-embedding-3-large @ 1536)
        │
        ▼
┌───────────────── Postgres + pgvector ──────────────────┐
│ documents · chunks · entities · chunk_entities         │
│ HNSW (dense) · GIN tsvector (sparse) · adjacency       │
│ query_audit · agent_memories · checkpoints · eval_runs │
└────────────────────────────────────────────────────────┘
        │
  RRF(dense, sparse) in one SQL statement
  + entity expansion (weighted third ranker)
  + Cohere rerank-v3.5 cross-encoder
        │
        ▼
  supervisor ─┬─ researcher ─┐
              └─ web_search ─┴─ answerability ─┬─ refuse ─┐
                                               └─ analyst ─ critic ─┬ retry ─▶ researcher
                                                                    └ approve ─┤
                                                                      compliance ─▶ audit
        │
        ▼
  FastAPI (async, SSE) · Prometheus · CloudWatch · ECS Fargate
```

### The six agents

| role | model | job |
|---|---|---|
| **Supervisor** | cheap | classify intent, rewrite the query, route corpus / web / both |
| **Researcher** | *none* | RRF retrieval — fusion is SQL, so no model call |
| **WebSearch** | *none* | current external facts (Tavily, primary sources only) |
| **Analyst** | full | grounded synthesis with inline `[chunk_id]` citations |
| **Critic** | cheap | verify claims against passages; can send retrieval back |
| **Compliance** | *none* | guardrails, PII, audit write |

The **Critic → Researcher edge is a cycle**, which is what makes this a graph
rather than a pipeline: a rejected draft returns for another retrieval, because
the usual cause of an unsupported claim is missing evidence rather than bad
phrasing. It is bounded — an unbounded self-correction loop is an unbounded
bill.

Only the Analyst uses the full model. Six agents cost **less per query** than the
original three-call pipeline, because deleting an LLM-based reranker paid for
the rest.

### Memory — four tiers, one database

| tier | scope | mechanism |
|---|---|---|
| working | one request | LangGraph `AgentState` |
| short-term | one thread | `PostgresSaver` checkpointer, keyed by `thread_id` |
| long-term | one org | `agent_memories` + pgvector, namespaced |
| episodic / audit | forever | `query_audit`, append-only |

The audit table is the compliance record *and* the source of real evaluation
data. A hand-written golden set measures regressions; production traffic
measures quality.

---

## Quick start

**Prerequisites:** Python 3.11+, Docker, an OpenAI key.

```bash
cp .env.example .env          # set OPENAI_API_KEY (COHERE_API_KEY recommended)

docker compose up -d postgres redis
pip install -e ".[dev]"
alembic upgrade head          # creates the pgvector schema

finance-rag index data/corpus
finance-rag ask "What is the four-part test for R&D tax credit qualification?"
```

> **Postgres is published on host port `5434`, not 5432.** A native PostgreSQL
> install commonly holds 5432, and when it does Docker can only bind the IPv6
> side — connections then land on the wrong server and report a *password*
> failure that has nothing to do with the password. Connect with
> `127.0.0.1:5434`, and use `127.0.0.1` rather than `localhost`, which resolves
> to both stacks.

### API

```bash
uvicorn finance_rag.api.app:app --reload --port 8000
```

| endpoint | purpose |
|---|---|
| `POST /v1/ask` | answer a question (`thread_id` for multi-turn, `as_of` for dated) |
| `POST /v1/ask/stream` | server-sent events: per-role progress, then the verified answer |
| `POST /v1/ask/multipart` | question plus an image |
| `POST /v1/index` | queue an indexing job → `202` + job id |
| `POST /v1/upload` | store a document durably, then queue indexing → `202` |
| `GET /v1/jobs/{id}` | job status |
| `GET /v1/audit` | append-only interaction record |
| `GET /v1/eval/runs` | evaluation run history |
| `GET /health` · `GET /metrics` | health, Prometheus |

Tenancy is resolved from an `X-Org-Id` header — a deliberate placeholder for
real authentication, isolated in one function so the swap touches one place.

---

## Retrieval: Reciprocal Rank Fusion

    RRF(d) = sum over rankers r of  w_r / (k + rank_r(d))       # k = 60

Only **rank position** feeds the fusion. `ts_rank_cd` is unbounded and cosine
lives in `[-1, 1]`, so a weighted score blend adds incomparable quantities.
Min-max normalising first is worse: it pins the best candidate to 1.0 whatever
its real quality, making a failed retrieval indistinguishable from a perfect
one — which is precisely why the original abstention threshold could never fire.

Dense + sparse fusion runs in a **single SQL statement**
(`PgVectorStore.hybrid_search_rrf`); entity expansion folds in as a
down-weighted third ranker on the same `1/(k+rank)` scale.

Because RRF scores are ordinal — top-1 is always ≈ 1/(k+1) — they carry **no
absolute meaning**. Abstention therefore reads `RetrievedChunk.cosine`, the raw
similarity returned alongside the fused score.

### Abstention is not a threshold

Measured on the golden set, answerable questions score cosine **0.591–0.799**
and unanswerable ones **0.554–0.735**. The distributions overlap almost
entirely, because cosine measures *topical similarity*, not whether a fact is
present. So a dedicated answerability gate runs between retrieval and
generation — and refusing there also skips the expensive generation call.

---

## Evaluation

```bash
python scripts/run_eval.py                 # full run (spends model credits)
python scripts/run_eval.py --no-judge      # retrieval metrics only, cheaper
python scripts/run_eval.py --save-baseline # pin current numbers as the baseline

python scripts/run_eval.py --list-runs     # run history (no model calls)
python scripts/run_eval.py --diff 2 3      # compare two runs
```

Every run persists to `eval_runs` / `eval_cases` with the git SHA and a snapshot
of the settings that shaped it — *a metric without its configuration is a
number, not a measurement*. `--diff` reports metric deltas, configuration
changes, **and the individual cases that flipped**: an average that moved says
go looking, a named case that changed says where.

| | reported | why |
|---|---|---|
| **Online** (per request) | latency, num_retrieved, top/mean/min cosine, rerank score, citation grounding | no relevance labels exist at request time |
| **Offline** (golden set) | hit rate, MRR, nDCG, precision@k, recall@k, faithfulness, abstention accuracy | labels available |

`hit_rate`, `mrr` and `ndcg` are **`None` online**. Deliberately: computing them
from the retriever's own scores makes them constants, not measurements.

The golden set is 39 cases across 8 service lines, **7 of them unanswerable** —
those assert the system *refuses*, which no ranking metric can catch. Labels key
on source basename or corpus `doc_id`, never generated `doc_id` (path hashes
differ per machine).

```bash
pytest -q     # 224 tests; store integration tests skip without a database
```

---

## Observability

| | where | use |
|---|---|---|
| **LangSmith** | SaaS | hosted convenience for development |
| **Langfuse** | self-hosted, in-VPC | the one that can point at production |

Tax queries and retrieved passages leave the deployment with a SaaS tracer,
which for this content needs sign-off. Langfuse runs beside the app instead,
behind a compose profile because it is four extra containers:

```bash
docker compose --profile langfuse up -d     # UI on http://localhost:3001
```

Each agent node becomes its own span, so a slow or looping request is readable
by role. Every path fails open: a missing key, unreachable host or broken
handler degrades to no tracing, never to no answers.

---

## Deployment

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # db_password, openai_api_key, alarm_email
terraform init && terraform apply
```

Then, **in this order** — CD pushes to an ECR repo and updates an ECS service
that must already exist:

1. `terraform apply`
2. GitHub → Settings → Secrets → `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
   (from `terraform output github_actions_access_key_id` / `..._secret_access_key`)
3. GitHub → Variables → `API_BASE_URL`, `UI_BUCKET`, `UI_DISTRIBUTION_ID`
   (from `terraform output`)
4. Push to `main` → **Build → Migrate → Deploy**, plus an independent UI job

Migrations run as a **one-shot ECS task inside the VPC**, not from the runner:
RDS is in private subnets and unreachable from GitHub Actions, and running them
on API container startup would race N tasks against one schema. A non-zero exit
stops the deploy rather than shipping code against a mismatched schema.

### What gets created

ECS Fargate · ALB · RDS PostgreSQL 16 + pgvector · S3 (uploads + UI) ·
CloudFront (UI **and** API) · ECR · SSM · CloudWatch + SNS alarms.

**HTTPS without a domain.** ACM will not issue a certificate for
`*.elb.amazonaws.com`, so CloudFront fronts the ALB and supplies a free
certificate. The ALB is locked to CloudFront two ways — a shared secret header
and the AWS-managed origin-facing prefix list — because the CloudFront→ALB hop
is HTTP.

**NAT gateway is optional and off**, the largest single saving in the stack
(~$32/mo). Tasks then run in public subnets with no inbound path except the ALB
security group. The trade is real and stated in the variable description.

Roughly **$41/month**. `terraform destroy` between demos beats every other
optimisation.

> Endpoint URLs come from `terraform output` and are deliberately **not** listed
> here. The API has no authentication yet, and a public URL is an open invitation
> to spend your model credits.

### UI

`web/` is a Next.js 15 client-rendered app, statically exported and served from
S3 behind CloudFront — no Node server. `NEXT_PUBLIC_API_URL` is inlined at build
time, so changing it requires a rebuild.

```bash
cd web && npm install && npm run dev     # local, against API on :8000
```

---

## Documentation

| | |
|---|---|
| [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) | Architecture, RRF, the six agents, memory, evaluation, guardrails — plus 26 war stories with real numbers |
| [`docs/AWS_AND_TERRAFORM.md`](docs/AWS_AND_TERRAFORM.md) | Every service and why, the NAT cost trade, Terraform patterns, CI/CD bootstrap, UI hosting |
| [`docs/ENTERPRISE_FEATURES.md`](docs/ENTERPRISE_FEATURES.md) | Redis semantic cache, LangSmith, multimodal ingestion |
| [`docs/TERRAFORM_AND_CICD.md`](docs/TERRAFORM_AND_CICD.md) | ⚠️ **stale** — written for the removed Neo4j Aura deployment |

## Project layout

```text
src/finance_rag/
  agent/         orchestrator (6-role graph) + specialist roles
  store/         Postgres/pgvector store, RRF in SQL
  retrieval/     hybrid retriever, RRF helpers
  parsing/       layout-aware PDF parsing
  chunking/      hierarchical, table-safe
  guardrails/    answerability gate, citation verification, PII
  memory/        checkpointer, long-term recall, audit trail
  evaluation/    run history and diffing
  storage/       S3 / local object store
  pipeline/      indexing and background jobs
migrations/      Alembic (0001–0004)
infra/terraform/ VPC, ECS, RDS, S3, CloudFront, alarms
.github/workflows/  ci · cd · eval
data/eval/       golden set + baseline
web/             Next.js UI
```

## Known gaps

- **No authentication.** Tenancy resolves from a header; this must become a
  verified token claim before any real exposure.
- **No WAF** in front of the ALB.
- **CloudFront→ALB is HTTP.** Mitigated by two locks; register a domain and
  attach an ACM certificate for end-to-end TLS.
- **Dense borderless tables** (the MACRS appendix grids) are still not
  cell-accurate. Column headers are recovered; rows merge. The quality gate
  leaves them as prose rather than emitting false structure.

## Responsible use

Outputs are **decision-support** for Source Advisors teams and partner CPA
firms — not formal tax or legal advice. Qualified professionals must review
client deliverables.
