# Architecture, end to end

One map of the whole system, then each path through it with the file that
does each step, then how to walk someone through it in ten minutes.

The other docs go deep on one layer each: [`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md)
on retrieval and the agents, [`AWS_AND_TERRAFORM.md`](AWS_AND_TERRAFORM.md)
on infrastructure and cost, [`DEPLOY_RUNBOOK.md`](DEPLOY_RUNBOOK.md) on
operating it, [`INTERVIEW_QA.md`](INTERVIEW_QA.md) on the trade-offs with
worked answers. This one is the map they hang on.

---

## 1. What it is

A retrieval-augmented question-answering service over a corpus of tax
advisory documents. A person asks a question in a browser; a six-role agent
graph routes it, retrieves and reranks evidence, decides whether it can be
answered, drafts a cited answer, criticises the draft, and records the whole
exchange. Every request is authenticated, attributed to a person or a
credential, rate-limited, traced, and audited.

Python 3.12, FastAPI, LangGraph, Postgres + pgvector, on ECS Fargate behind
CloudFront, with a static Next.js UI. Terraform for all of it.

---

## 2. The map

```
                              ┌──────────────────────────────┐
                              │  Cognito (optional)          │
                              │  hosted login · PKCE client  │
                              └──────────────┬───────────────┘
                                             │ ID token
  Browser ──────────────────────────────────►│
     │                                       ▼
     │  HTTPS                    ┌─────────────────────────┐
     ▼                           │  UI (Next.js, static)   │
  ┌──────────┐   ┌──────────┐    │  sign-in gate → dashboard│
  │ WAF      │──►│CloudFront│───►│  S3 bundle              │
  │ per-IP   │   │  (UI)    │    └─────────────────────────┘
  │ limits,  │   └──────────┘
  │ managed  │   ┌──────────┐    Authorization: Bearer <key | token>
  │ rules    │──►│CloudFront│──── X-Origin-Verify ────┐
  └──────────┘   │  (API)   │                         ▼
                 └──────────┘                    ┌─────────┐  CloudFront prefix list only
                                                 │   ALB   │  secret header or 403
                                                 └────┬────┘
                                                      ▼
  ┌───────────────────────────────── ECS Fargate task ──────────────────────────────────┐
  │                                                                                     │
  │  ┌─ api (0.5 vCPU / 1 GiB, non-root, no pip) ────────────────────────────────────┐  │
  │  │                                                                               │  │
  │  │  authenticate()  ── API key (SHA-256 table) or JWT (JWKS) ──► Principal       │  │
  │  │  require(scope)  ── ask | index | read | metrics                              │  │
  │  │  enforce()       ── per-credential, per-scope rate limit (Redis or local)     │  │
  │  │                                                                               │  │
  │  │  MultiAgentRAG (LangGraph, one span per node)                                 │  │
  │  │    supervisor ─► researcher ─► answerability ─► analyst ─► critic ─► compliance│  │
  │  │       │              │              │              │          │          │     │  │
  │  │    cheap LLM     RRF in SQL     cheap LLM      full LLM   cheap LLM   audit row│  │
  │  │                  + Cohere rerank                                              │  │
  │  │                                                                               │  │
  │  │  PostgresSaver   ── multi-turn memory, keyed by thread_id                     │  │
  │  │  track_request   ── 5 metrics → CloudWatch, one PutMetricData                 │  │
  │  └───────────────────────────────────────────────────────────────────────────────┘  │
  │  ┌─ aws-otel-collector (sidecar, non-essential) ─── OTLP :4318 ──► X-Ray ─────────┐  │
  │  └───────────────────────────────────────────────────────────────────────────────┘  │
  └─────────────────────────────────────────────────────────────────────────────────────┘
           │                    │                      │                    │
           ▼                    ▼                      ▼                    ▼
    ┌─────────────┐     ┌──────────────┐      ┌─────────────────┐   ┌─────────────┐
    │ RDS Postgres│     │ S3 uploads   │      │ CloudWatch      │   │ OpenAI      │
    │ + pgvector  │     │ (versioned)  │      │ logs · metrics  │   │ Cohere      │
    │ chunks,     │     └──────────────┘      │ alarms → SNS    │   │ (egress,    │
    │ audit,      │                           └─────────────────┘   │  no NAT)    │
    │ memory, jobs│                                                 └─────────────┘
    └─────────────┘

  Separate tasks from the same image:   migrate (alembic, before every deploy)
                                        index   (2 vCPU / 8 GiB, on /v1/index)
                                        eval    (golden set, on release PRs + weekly)
```

---

## 3. A question, step by step

Each step names the file that does it.

| # | step | where |
|---|---|---|
| 1 | Browser has a credential: an ID token from the sign-in gate, or a pasted API key. Both live in `sessionStorage`, both go out as `Authorization: Bearer`. | [`web/src/lib/oidc.ts`](../web/src/lib/oidc.ts), [`web/src/lib/api.ts`](../web/src/lib/api.ts) |
| 2 | WAF at the CloudFront edge: per-IP rate limit and AWS managed rules, before authentication, before a task spends CPU. | [`infra/terraform/waf.tf`](../infra/terraform/waf.tf) |
| 3 | CloudFront forwards to the ALB with a secret header; the ALB's listener rule 403s without it, and its security group admits only the CloudFront prefix list. The ALB is internet-facing so CloudFront can reach it, and unreachable by anyone else. | [`api_cdn.tf`](../infra/terraform/api_cdn.tf), [`alb.tf`](../infra/terraform/alb.tf) |
| 4 | `authenticate()` resolves the bearer. A JWT is validated against the provider's JWKS with issuer and audience pinned; an API key is looked up by SHA-256 digest. Either way: a `Principal` with tenant, scopes, and a subject. | [`api/auth.py`](../src/finance_rag/api/auth.py) |
| 5 | `require(Scope.ASK)` checks the scope, then `enforce()` counts the request against a per-credential window — Redis when configured, so the limit holds across autoscaled tasks. | [`api/auth.py`](../src/finance_rag/api/auth.py), [`api/ratelimit.py`](../src/finance_rag/api/ratelimit.py) |
| 6 | The handler hops to a worker thread. The agent blocks on several model calls; awaiting it on the event loop would stall every other request. | [`api/app.py`](../src/finance_rag/api/app.py) |
| 7 | **Supervisor** routes and rewrites the query (cheap model). | [`agent/orchestrator.py`](../src/finance_rag/agent/orchestrator.py) |
| 8 | **Researcher** runs hybrid retrieval: BM25 and vector ranks fused by RRF in one SQL round trip, scoped to the tenant, then Cohere cross-encoder rerank. | [`store/pgvector_store.py`](../src/finance_rag/store/pgvector_store.py), [`retrieval/`](../src/finance_rag/retrieval/) |
| 9 | **Answerability** decides whether the evidence supports an answer (cheap model). A refusal here is a separate stage, not a similarity threshold — RRF fuses ranks and discards the confidence signal a threshold would need. | [`guardrails/answerability.py`](../src/finance_rag/guardrails/answerability.py) |
| 10 | **Analyst** drafts the cited answer (the one full-model call). | [`agent/graph.py`](../src/finance_rag/agent/graph.py) |
| 11 | **Critic** checks claims against evidence and can send the researcher back, bounded by `critic_max_retries` — an unbounded self-correction loop is an unbounded bill. | [`agent/orchestrator.py`](../src/finance_rag/agent/orchestrator.py) |
| 12 | **Compliance** verifies every cited id exists in the retrieved set — a fabricated citation is an answer that cannot be audited — and writes the audit row with `user_id` = the principal's subject. | [`memory/audit.py`](../src/finance_rag/memory/audit.py) |
| 13 | `track_request` pushes latency, count, top cosine, refusals and hallucinated citations to CloudWatch in one `PutMetricData`. Each graph node ran inside a span; the sidecar has already forwarded them to X-Ray. | [`monitoring/metrics_emit.py`](../src/finance_rag/monitoring/metrics_emit.py), [`tracing.py`](../src/finance_rag/tracing.py) |
| 14 | If a `thread_id` was given, `PostgresSaver` checkpointed the state, so "and for 2023?" resumes rather than starts cold. | [`memory/threads.py`](../src/finance_rag/memory/threads.py) |

Fourteen steps, three model calls at full price → one. Six roles cost less than
the original three-call pipeline because reranking left the LLM for a
cross-encoder and the rewrite folded into routing.

---

## 4. Signing in

```
sign-in gate ──► Cognito hosted UI ──► redirect back with ?code&state
       ▲                                        │
       │                                        ▼
       │              PKCE: verifier never left the tab; code is useless without it
       │                                        │
       └──── expired ◄── sessionStorage ◄── ID token stored where an API key would go
```

- The **gate** is the front door when a provider is configured; the dashboard
  is behind it. An API key still opens it, folded into a collapsed section.
- **PKCE** because a static export has no server and therefore nowhere to hold
  a client secret. The client id is public by construction.
- The **ID token**, not the access token, for Cognito: its access tokens carry
  `client_id` rather than `aud`, and the API pins audience.
- The token is copied into the credential slot **with its kind recorded**, so
  expiry clears an OIDC token and leaves a pasted key alone. The bug that
  motivated this: an expired token left in the slot, a gate that saw "a
  credential", and a dashboard on which every request 401'd.
- The API rejects a token with **no tenant claim** rather than defaulting it.
  An unattributable request landing in the default org is exactly what the
  tenancy column exists to prevent.

---

## 5. From push to production

```
git push origin main
        │
        ▼  (nothing runs on push)
open PR main → master
        │
        ├── CI ────────── ui: vitest + next build
        │                 test: lock drift · ruff · mypy · alembic · pytest (real pgvector)
        ├── Security ──── gitleaks (full history) · pip-audit (the lock) · trivy fs+IaC · CodeQL ×2
        └── Eval ──────── golden set, no judge, fails on 0.05 regression   ← required check
        │                 (skipped, not absent, for docs/UI-only changes)
        ▼
merge  (ruleset: PR required, checks required, no bypass)
        │
        ▼
CD on master
        ├── build image
        ├── trivy image scan ─── between build and push: a vulnerable image never reaches ECR
        ├── push to ECR
        ├── migrate ──────────── one-shot ECS task, alembic upgrade head, inside the VPC
        │                        non-zero exit stops the deploy
        ├── deploy ───────────── render task def with the new image → rolling update → wait stable
        └── ui ───────────────── next build with OIDC vars → S3 sync → CloudFront invalidate
```

Three things make this a pipeline rather than a script:

- **`main` is where work goes; `master` is where releases go.** CD once fired on
  every push to `main`, which made deploying indistinguishable from saving.
- **Every gate can fail.** No `|| true` anywhere. A gate that cannot fail is
  not a gate; the eval gate that skips UI changes reports *skipped*, which
  the ruleset accepts, rather than never reporting, which it does not.
- **Migrations run in the VPC, once, before the deploy.** Not on the runner —
  RDS is private and unreachable from it. Not on container startup — N tasks
  would race N migration runs against one schema.

---

## 6. Indexing

```
POST /v1/index  (scope: index)
   │
   ├── job row (queued) ──► 202 + job_id, immediately
   │
   └── ecs:RunTask ──► index task, 2 vCPU / 8 GiB, same image
                          │
                          ├── pdfplumber: headings from font geometry, tables as blocks → markdown
                          ├── chunker: split on headings, tables kept atomic, token-budgeted children
                          ├── OpenAI embeddings, batched
                          └── chunks → RDS; job row → succeeded
GET /v1/jobs/{id} polls the row
```

Its own task because parsing a 113-page PDF in the 0.5 vCPU API container
was SIGKILLed — exit 137. Its own image tag because it is the same code.

---

## 7. Where everything is observable

| signal | where it goes | what reads it |
|---|---|---|
| Structured JSON logs | CloudWatch `/ecs/source-advisors-finance-rag` | Logs Insights, `aws logs tail` |
| Request latency, count, top cosine, refusals, hallucinated citations | CloudWatch `FinanceRAG/SourceAdvisors` | dashboard, the `RequestLatencyMs` p95 alarm |
| ECS CPU/memory, ALB 5xx | `AWS/ECS`, `AWS/ApplicationELB` | dashboard, two alarms |
| Spans — one per graph node | X-Ray, via the ADOT sidecar | X-Ray traces, filtered by service name |
| Agent internals — prompts, tokens | LangSmith / Langfuse | their UIs |
| Prometheus `/metrics` | nothing in AWS | local `docker compose` only |
| Alarms | SNS → `alarm_email` | a person, once the subscription is confirmed |

Three layers, three tools, on purpose: CloudWatch says a request was slow,
X-Ray says *which stage*, LangSmith says *what the model saw*. The trace id
is what joins them.

---

## 8. The ten-minute walkthrough

The order that makes each decision motivate the next. One sentence per layer;
the follow-ups are in [`INTERVIEW_QA.md`](INTERVIEW_QA.md).

1. **The problem.** Tax advisory questions over a private corpus, where a
   wrong-but-plausible answer is worse than a refusal — so abstention and
   citation verification are stages, not prompt instructions.
2. **Retrieval.** BM25 and vector fused by RRF in one SQL statement, because
   scores from the two are not comparable and a weighted blend is a constant
   overfit to today's corpus. Reranked by a cross-encoder, which moved 2–3k
   tokens per query off the LLM.
3. **The graph.** Six roles, cycles bounded, and cheaper than the three-call
   pipeline it replaced — because "number of agents" is not a cost unit,
   tokens through expensive models is.
4. **One database.** Chunks, embeddings, audit, memory, jobs. RRF needs both
   ranks in one query; a dedicated vector store makes fusion an application
   join. The honest boundary is the low millions of chunks.
5. **Identity.** API keys for machines, OIDC tokens for people, one
   `Principal` either way. Scopes split by consequence — `ask` spends money,
   `index` rewrites the corpus, `read` exposes other people's questions. The
   audit row records *who*, not just *which key*.
6. **Bounding spend.** Per-credential rate limits with shared state, because
   per-process counters rise when you scale out. WAF at the edge for the
   floods that never reach a credential.
7. **The deploy.** `main` for work, `master` for releases; every gate can
   fail; migrations in the VPC, once, before the rollout; the image scanned
   between build and push.
8. **Cost.** ~$41/month for the demo by deleting the NAT gateway — and what
   that gave up: a defence-in-depth layer, named in `.trivyignore.yaml` with
   the reason. ~$85 for the production profile, each line justified.
9. **What broke.** Multi-turn memory was silently off in every deployed image
   for weeks because two dependencies were never declared; `/health` needed
   an OpenAI key and would have crash-looped the service on a rotated
   credential; an expired token left the sign-in gate open to a dashboard
   where nothing worked. Each found by a gate that had just been turned on.
10. **What is still open.** A domain for TLS to the origin. The restore
    drill, which is a procedure until it has been run. Say these unprompted.

---

## 9. Interview questions, by layer

Worked answers for most of these are in [`INTERVIEW_QA.md`](INTERVIEW_QA.md);
the retrieval and agent ones in [`SYSTEM_DESIGN.md` §9](SYSTEM_DESIGN.md);
the infrastructure ones in [`AWS_AND_TERRAFORM.md` §8](AWS_AND_TERRAFORM.md).
Listed here so the whole surface is in one place.

**Retrieval and generation**
- Why RRF over a weighted blend, what does `k` control, and what can RRF not express?
- Why is abstention a separate stage rather than a cosine threshold?
- `precision@k` is 0.225 and `hit_rate` is 0.906 — which is broken?
- How do you chunk a table, and what goes wrong if you split one?
- Why did adding three agents make each request cheaper?
- How do you stop a critic→researcher loop from running forever?

**Identity and authorization**
- How do you attribute a request to a person when the credential is shared?
- Why reject a token with no tenant claim instead of defaulting it?
- Why refuse to start with a JWKS URL but no issuer and audience?
- 503 for an unreachable JWKS, 401 for a bad token — why the distinction?
- A scanner says SHA-256 is too weak for your API keys. Is it right?
- Why PKCE, and why is the client id public?
- Why does `/metrics` get its own scope rather than `read`?

**Bounding cost and abuse**
- How do you rate limit a service that autoscales?
- Fixed window or sliding log, and what does the choice cost?
- The limiter's backing store is down — fail open or closed, and why?
- Your API already rate-limits. Why a WAF?

**Data and state**
- Why one Postgres rather than a vector database, and where does that stop being true?
- Where do migrations belong in a deploy pipeline? (Three homes, two wrong.)
- Why `ignore_changes` on `desired_count` and `task_definition`?
- What is your RTO, and how do you know?
- What is *not* backed up, and what is the plan for it?

**Delivery**
- Every commit to main deployed to production. What changes, and what does YAML alone not enforce?
- A required check shows "waiting for status" forever. What happened?
- Your lock-drift check fails every time PyPI publishes anything. Why, and what is the fix?
- The image scan blocks the deploy on packages that are not in your lock. Where are they?
- You turn on a type checker and it finds 29 errors. Now what — and what did paying it down find?
- How do you gate a deploy on RAG quality without paying for it on every push?

**Operations**
- CloudWatch says 160 seconds. Where did they go?
- What does a trace show that a latency metric cannot, and what had to be true for X-Ray to show anything?
- `/health` returned 500 in CI for weeks and nobody noticed. Why not, and what was the production risk?
- A feature was silently off in every deployed image. How did the fail-soft hide it, and what makes it visible now?
- What is exit code 137, and what did you change?
- A gauge reads flat and low. What are the two readings?

**Infrastructure and cost**
- Why Fargate over Lambda for this workload?
- What is the largest line item in a small ECS stack, and what did removing it give up?
- Why is the ALB internet-facing if only CloudFront may reach it?
- Why does CloudFront talk HTTP to the origin, and what would it take to change?
- Why are the production settings a separate file rather than the defaults?
- Where does Terraform state live, and why is one module allowed to keep local state?

**Security posture**
- Four scanners — what does each see that the others cannot?
- Of the last three findings, one was suppressed, one was accepted with a reason, one led to removing pip from the image. Which was which, and why?
- Why is the container non-root and without a package installer?
- Why are secrets in Parameter Store rather than the task definition, and what does that make key rotation?
