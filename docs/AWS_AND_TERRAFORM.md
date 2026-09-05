# AWS & Terraform

Infrastructure for FinanceRAG: what runs, why it was chosen, what was rejected,
and what it costs.

The theme throughout is **cost as a design constraint, not an afterthought**.
The single largest saving came from deleting one resource, not from tuning nine.

---

## 1. Services

| service | role | why this and not the alternative |
|---|---|---|
| **ECS Fargate** | runs the API container | No EC2 to patch. Lambda rejected: 5–15s LLM latency, cold starts, and a 15-min ceiling that indexing would breach |
| **ALB** | TLS termination, health checks, autoscaling signal | Needed for `awsvpc` + target-group health. API Gateway rejected: 30s hard timeout kills streaming |
| **RDS PostgreSQL + pgvector** | vectors, full-text, entities, memory, audit, eval history | One store for six jobs. OpenSearch/Pinecone rejected: another service, another bill, no ACID |
| **S3** | uploaded documents | Task disk is ephemeral — the bug that motivated it |
| **ECR** | container images | Lifecycle policy keeps 10 images |
| **SSM Parameter Store** | secrets injected at runtime | Free at this scale. Secrets Manager rejected: ~$0.40/secret/month for rotation we don't use |
| **CloudWatch** | logs, metrics, alarms, dashboard | Already integrated with ECS |
| **SNS** | alarm delivery | Without it alarms change state and tell nobody |
| **VPC endpoint (S3)** | private S3 access | Gateway endpoints are free and avoid NAT charges |

### Deliberately not used

- **Lambda** — latency and duration limits are wrong for this workload
- **API Gateway** — 30s timeout is incompatible with SSE streaming
- **DynamoDB for state locking** — S3 native locking (`use_lockfile`) replaces it
- **ElastiCache** — Redis semantic cache is optional; ~$11/mo for a feature that
  masks retrieval behaviour during evaluation
- **Secrets Manager** — SSM SecureString is free and sufficient

---

## 1b. IAM: four identities, each scoped to one job

Least privilege here is not decoration -- two of these were caught granting too
little, which is the direction that produces a broken deploy rather than a
breach, but the reasoning is the same.

| identity | type | holds | why it exists |
|---|---|---|---|
| `...-exec` | role | pull from ECR, read `/{project}/*` in SSM, write logs | **execution role**: what the ECS *agent* uses to start a container |
| `...-task` | role | S3 read/write on the uploads bucket, `cloudwatch:PutMetricData`, `ecs:RunTask` + `iam:PassRole` | **task role**: what the *application* uses at runtime |
| `...-gha-user` | user | ECR push, ECS register/run/update, `iam:PassRole`, S3 sync + CloudFront invalidation | CI/CD deploy identity |
| `...-github-actions` | role | OIDC trust (unused; access keys proved more reliable for this account) | kept for the OIDC path |

**Execution role vs task role** is the distinction interviewers probe. The
execution role is used *before* your code runs -- pulling the image, resolving
secrets into environment variables. The task role is assumed *by* your code.
Secrets injection needs the execution role; calling S3 from Python needs the
task role. Confusing them produces a container that starts and then cannot do
anything, or one that never starts at all.

**`iam:PassRole` is the non-obvious one.** The API launches indexing tasks, and
those tasks assume the execution and task roles. AWS requires the caller to hold
`iam:PassRole` for exactly the roles being passed -- otherwise anyone able to
call `RunTask` could launch a task wearing a more privileged role. Omitting it
fails `RunTask` with an authorization error that does not mention PassRole.

**Two permissions CI needed that were not obvious:**

- `ecs:TagResource` -- the provider applies `default_tags` to everything, so a
  re-registered task definition carries tags and registering it requires
  permission to tag. `RegisterTaskDefinition` alone is refused.
- `ecs:RunTask` -- absent entirely at first. The deploy could build and update a
  service but not run the migration task, which is the step that must precede
  the deploy.

## 1c. Three task definitions, one image

| family | shape | command | why separate |
|---|---|---|---|
| `...-api` | 0.5 vCPU / 1 GiB | `uvicorn` | request serving is IO-bound on model APIs |
| `...-migrate` | 0.5 vCPU / 1 GiB | `alembic upgrade head` | must run inside the VPC; RDS is private |
| `...-index` | **2 vCPU / 8 GiB** | `finance-rag run-job <id>` | parsing is CPU- and memory-bound |

The sizing difference is not a preference. Indexing this corpus inside the API
container was SIGKILLed with **exit 137** every time -- pdfplumber holds the full
page model for a 113-page publication. It completed at 8 GiB. Same image, three
shapes, because *serving* and *parsing* are different workloads.

## 2. The NAT gateway decision

This is the most instructive cost decision in the stack, and a good interview
topic because the trade is real rather than free.

**The problem.** ECS tasks in private subnets need outbound internet to reach
OpenAI, Cohere and Tavily. The default answer is a NAT gateway: **~$32/month
plus per-GB data processing** — roughly the cost of everything else combined,
for one purpose.

**The alternative.** Run tasks in public subnets with `assign_public_ip = true`
and a security group admitting **only** the ALB. The task has a public IP for
egress; nothing can reach it inbound except the load balancer.

```hcl
subnets          = var.enable_nat_gateway ? aws_subnet.private[*].id : aws_subnet.public[*].id
assign_public_ip = !var.enable_nat_gateway
```

**The honest trade:** with NAT, a security-group misconfiguration exposes
nothing because there is no route in. Without it, the security group is the only
boundary. That is a smaller margin for error, and for a workload under an
obligation that says "no public interface" you keep NAT and pay.

Default here is `false`, with the trade written into the variable description
rather than hidden in a commit message.

**Interview answer:** "We made it a variable because it's a security/cost trade,
not a best practice. Defaulting to off saved ~40% of the bill; the description
states what you give up so nobody flips it without knowing."

---

## 3. Cost

Rough monthly, `ap-south-1`. Verify against current pricing.

| | before | after | how |
|---|---|---|---|
| NAT gateway | ~$32 | **$0** | public subnets, SG-locked |
| Fargate | ~$30 (2 × 1 vCPU / 2 GB) | ~$8 (1 × 0.5 / 1 GB) | work is IO-bound on model APIs |
| ALB | ~$18 | ~$18 | required |
| RDS `db.t4g.micro` | — | ~$13 | Graviton, single-AZ |
| S3 + CloudWatch + ECR | — | ~$2 | |
| **total** | **~$100+** | **~$41** | |

**The largest optimisation isn't in this table:** `terraform destroy` between
demos. A stack that isn't running costs nothing, and this one rebuilds in
minutes.

### Per-query cost, for completeness

Application-level cost engineering mattered more than infrastructure:

| | before | after |
|---|---|---|
| query rewrite | full model | folded into the router (cheap) |
| **reranking** | **full model, 2–3k tokens/query** | **hosted cross-encoder** |
| generation | full model | full model |
| answerability + criticism | — | cheap model |
| **net** | **3 × full** | **1 × full + 2 × cheap** |

Six agents cost less per query than the original three-call pipeline.

---

## 4. Security posture

**Secrets never touch the image.** SSM SecureString parameters are injected as
environment variables at task start. The execution role is scoped to
`/${project}/*` rather than `Resource = "*"` — an over-broad grant that was
tightened during this work.

**Optional secrets are conditional resources.** SSM rejects empty values, so:

```hcl
resource "aws_ssm_parameter" "cohere_api_key" {
  count = var.cohere_api_key != "" ? 1 : 0
  ...
}
```

and the container's `secrets` list is built with `concat()`, so an unset key
means the variable simply isn't present rather than present-and-empty.

**Database is unreachable from the internet.** `publicly_accessible = false`,
in private subnets, with a security group admitting only the ECS task security
group on 5432.

**S3**: public access blocked at the bucket level, AES256 at rest, versioned
with 90-day noncurrent expiry. The task role can read/write **only** that
bucket.

**TLS**: `ELBSecurityPolicy-TLS13-1-2-2021-06` drops TLS 1.0/1.1, which fail
most compliance baselines. HTTP 301-redirects once a certificate is configured.

**Authentication**: `/v1` requires a bearer credential, supplied to the task as
the `AUTH_API_KEYS` SecureString and never as a task-definition environment
variable — those are readable by anyone holding `ecs:DescribeTaskDefinition`.
Each key binds an org and a scope set, so the org is a property of the verified
credential rather than of a client header. With `auth_enabled = true` and no keys
configured the task **fails its lifespan and never becomes healthy**: an API that
bills per call should crash-loop rather than serve traffic it cannot attribute.

### Known gaps

- **The browser UI cannot hold a key** — the static export inlines
  `NEXT_PUBLIC_*` into a public bundle. Machine clients are unaffected; a shared
  UI deployment needs a key-entry control or an authenticating proxy.
- **Key rotation is a redeploy.** One SSM parameter, read at task start.
- **No WAF.** Worth adding before public exposure.
- **CD does not run migrations** — see §6.

---

## 5. Terraform patterns worth explaining

### Optional resources

```hcl
resource "aws_nat_gateway" "nat" {
  count = var.enable_nat_gateway ? 1 : 0
  ...
}

resource "aws_route_table" "private" {
  dynamic "route" {
    for_each = var.enable_nat_gateway ? [1] : []
    content { cidr_block = "0.0.0.0/0", nat_gateway_id = aws_nat_gateway.nat[0].id }
  }
}
```

`count` toggles the resource; `dynamic` toggles a **block inside** a resource —
you cannot use `count` for that.

### Remote state

```hcl
backend "s3" {
  bucket       = "source-advisors-finance-rag-tfstate"
  key          = "finance-rag/terraform.tfstate"
  region       = "ap-south-1"
  encrypt      = true
  use_lockfile = true      # S3 native locking; no DynamoDB table needed
}
```

Local state means no locking, no history, and a stack only one person can safely
change. `use_lockfile` is the modern replacement for the DynamoDB lock table.

### `ignore_changes` on the ECS service

```hcl
lifecycle {
  ignore_changes = [desired_count, task_definition]
}
```

Because **CI/CD owns those**. Autoscaling changes `desired_count`; the deploy
workflow changes `task_definition`. Without this, every `terraform apply` would
roll the service back to the last image Terraform knew about.

This is the single most common Terraform/CD conflict and a good interview
answer: *"Terraform owns the shape of the infrastructure; the deploy pipeline
owns what's running in it."*

---

## 6. CI/CD

| workflow | trigger | does |
|---|---|---|
| `ci.yml` | every push / PR | ruff + full test suite against a pgvector service container |
| `cd.yml` | push to `main` | build → ECR → render task def → ECS rolling deploy |
| `eval.yml` | manual + weekly | golden-set eval with a regression gate |

### CI runs a real database

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
```

So the store integration tests **execute** rather than skip. Tests that skip in
CI are tests you do not have.

### The lint gate is real

`ruff check src tests scripts migrations` — no `|| true`. That trailing
`|| true` was in the original workflow, which meant lint could never fail a
build. **A gate that cannot fail is not a gate.**

### Bootstrap order

1. **`terraform apply` first** — CD pushes to an ECR repo and updates an ECS
   service that must already exist.
2. **Add GitHub secrets** from `terraform output`:
   `github_actions_access_key_id` and `github_actions_secret_access_key`.
3. **Push to `main`** → CD builds, pushes, and rolls with
   `wait-for-service-stability`.

### Gap: migrations

CD deploys code but does not run `alembic upgrade head`. A deploy shipping code
that expects tables which don't exist is a broken deploy. Two options:

- **A migration step in CD** before the ECS deploy (simple; races if two deploys
  overlap)
- **A one-shot ECS task** running migrations, which the deploy waits on
  (correct; more moving parts)

The local `docker-compose` already models the right pattern — a separate
`migrate` service the API depends on — kept out of the API container precisely
so scaling to N tasks never races N concurrent migration runs.

---

## 7. The Next.js UI

`web/` is a Next.js 15 app that talks to the API through `NEXT_PUBLIC_API_URL`.
It is **client-rendered**, which means no Node server is required.

| option | cost | notes |
|---|---|---|
| **S3 + CloudFront** | ~$1–3/mo | Best fit. Add `output: 'export'` |
| Amplify Hosting | ~$5–15/mo | Git-push deploys, previews, no Terraform |
| Vercel | free tier | Fastest; UI leaves AWS while data stays |
| Second ECS service | ~$8/mo | Only if SSR or server actions are added |

**Recommended: S3 + CloudFront** — cheapest, no servers, ~40 lines of Terraform.

Two things that break a UI deployment regardless of host:

1. **CORS.** `allow_origins=["*"]` with `allow_credentials=True` is an invalid
   combination browsers reject. Pin to the real UI origin.
2. **Mixed content.** An HTTPS UI cannot call an HTTP API — the browser blocks
   every request silently. `acm_certificate_arn` must be set before the UI goes
   live.

---

## 7b. End-to-end flows

### A question, from browser to answer

```
Browser
  │  HTTPS
  ▼
CloudFront (UI)  ──▶ S3 static bundle
  │
  │  fetch(NEXT_PUBLIC_API_URL)   ← inlined at build time
  ▼
CloudFront (API)  ──HTTP + X-Origin-Verify──▶ ALB ──▶ ECS task
  │                                            │
  │                            listener rule: secret header or 403
  │                            security group: CloudFront prefix list only
  ▼
FastAPI  ──run_in_threadpool──▶ MultiAgentRAG
  │
  ├─ Supervisor      route + rewrite            (cheap model)
  ├─ Researcher      RRF in one SQL statement   (RDS, no model call)
  ├─ Cohere rerank   cross-encoder
  ├─ Answerability   can this be answered?      (cheap model)
  ├─ Analyst         grounded answer            (full model)
  ├─ Critic          verify claims              (cheap model)
  └─ Compliance      guardrails + audit row     (no model)
  │
  ▼
answer + citations  ──▶ query_audit (append-only)
```

Every hop is deliberate: HTTPS at both edges, the ALB unreachable except through
CloudFront, the blocking agent off the event loop, and the audit row written
before the response returns.

### A deploy, from push to running

```
git push main
  │
  ▼
CI            ruff + 238 tests against a pgvector service container
  │
  ▼
CD ─ Build    docker build ──▶ ECR (tagged with the commit sha)
   │
   ├─ Migrate  ECS RunTask: alembic upgrade head, inside the VPC
   │             └─ non-zero exit stops the deploy
   │
   ├─ Deploy   render task def with the new image ──▶ ECS rolling update
   │             └─ wait-for-service-stability
   │
   └─ UI       npm ci && next build (static export)
                 ──▶ S3 sync ──▶ CloudFront invalidation
```

Migrations run **inside the VPC** because RDS is private and unreachable from a
GitHub runner, and **not** on API container startup because N tasks would race N
migration runs against one schema.

### Indexing, from request to rows

```
POST /v1/index
  │
  ▼
create job row (queued) ──▶ ecs:RunTask ──▶ index task (2 vCPU / 8 GiB)
  │                                            │
  │  202 + job_id returned immediately         ├─ marks row running
  │                                            ├─ parse · chunk · embed
  ▼                                            ├─ writes chunks to RDS
GET /v1/jobs/{id}  ◀── polls the row           └─ marks succeeded / failed
```

The **task** updates the row, not the caller, so the row records what happened
rather than what was dispatched. A dispatch failure marks the row failed rather
than leaving it queued for a worker that does not exist, and the next container
start reaps anything a killed worker abandoned.

---

## 8. Interview questions this covers

**Architecture**
- Why Fargate over Lambda for an LLM workload? *(Latency, duration ceiling, cold starts.)*
- Why one Postgres rather than a dedicated vector database?
- Why not API Gateway? *(30s timeout vs streaming.)*

**Cost**
- What's the largest line item in a small ECS stack, and how do you remove it?
- What did you give up removing it? *(A defence-in-depth layer — say so.)*
- How do you reduce per-query LLM cost without reducing quality?

**Terraform**
- `count` vs `dynamic` — when do you need each?
- Why `ignore_changes` on `desired_count` and `task_definition`?
- How do you handle a secret that may legitimately be empty?
- Why does remote state matter before a second person joins?

**IAM**
- Execution role vs task role — what uses each, and when does confusing them
  bite? *(Secrets injection needs the execution role; calling S3 from your code
  needs the task role.)*
- Why does `RunTask` require `iam:PassRole`? *(Otherwise anyone able to call it
  could launch a task wearing a more privileged role.)*
- Why did `RegisterTaskDefinition` fail even though the action was allowed?
  *(default_tags means the definition carries tags, so tagging permission is
  required too.)*

**Sizing**
- Why three task definitions from one image?
- What does exit code 137 mean, and what would you change? *(SIGKILL / OOM —
  and the fix was a differently-sized task, not a bigger service.)*
- Why is the API 0.5 vCPU while indexing is 2 vCPU / 8 GiB?

**Security**
- How do secrets reach a container without being in the image?
- What breaks if the ECS security group is misconfigured, with and without NAT?
- Why 404 instead of 403 for another tenant's resource? *(403 confirms it exists.)*

**CI/CD**
- Why must `terraform apply` precede the first deploy?
- Where do database migrations belong in a deploy pipeline?
- Why run a real database in CI instead of mocking it?

---

## 9. Related

- [`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md) — architecture, agents, evaluation, war stories
- [`TERRAFORM_AND_CICD.md`](TERRAFORM_AND_CICD.md) — *(stale: written for the Neo4j Aura deployment)*
