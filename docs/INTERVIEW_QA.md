# Trade-offs and scenarios

Answer-bearing companion to the question lists in
[`SYSTEM_DESIGN.md` §9](SYSTEM_DESIGN.md) and
[`AWS_AND_TERRAFORM.md` §8](AWS_AND_TERRAFORM.md). Those name the questions;
this one works through them.

Two rules that make these answers land:

- **Name what you gave up.** Every choice below cost something. An answer that
  presents a decision as free reads as marketing, and the follow-up you get is
  the one you did not prepare for.
- **Numbers beat adjectives.** "Cheaper" is unfalsifiable; "$100/mo to $41/mo by
  deleting the NAT gateway" invites the real question, which is what that cost
  you.

---

## Part 1 — Trade-offs

### Why one Postgres instead of a dedicated vector database?

**Chose:** Postgres with pgvector holding chunks, embeddings, job rows, agent
checkpoints and the audit trail in one place.

**Gave up:** ANN index tuning at the level Pinecone or Weaviate offer, and a
scaling ceiling that arrives earlier than theirs.

**Why it was right here:** the corpus is 972 chunks across 16 documents. At that
size the index is not the bottleneck, and RRF needs BM25 and vector ranks
**fused in one query**. Split across two systems, fusion becomes an application
join over two network calls with no transaction around them. One database also
means one backup, one failover story, and one place where "did this answer cite
a chunk that exists?" is a join rather than a reconciliation job.

**The honest boundary:** this stops being true somewhere in the low millions of
chunks, when `ivfflat`/`hnsw` tuning starts to matter more than the join does.

### Why Fargate rather than Lambda?

**Gave up:** scale-to-zero. Idle Fargate costs ~$8/mo; idle Lambda costs nothing.

**Why:** a multi-agent RAG request makes several sequential model calls.
Lambda's duration ceiling and cold starts both land badly on that, and API
Gateway caps at 30 seconds, which forecloses streaming. The workload is IO-bound
on model APIs rather than CPU-bound — which is also why the API task is only
0.5 vCPU.

**Follow-up to expect:** *"So why is indexing a separate 2 vCPU / 8 GiB task?"*
Because parsing a 113-page PDF inside the 0.5 vCPU / 1 GiB API container gets it
SIGKILLed — exit 137. The fix was a differently shaped task, not a bigger
service. Three task definitions, one image.

### Why delete the NAT gateway?

**Chose:** tasks in public subnets, locked by security group. NAT was ~$32 of a
~$100 bill — the single largest line item.

**Gave up:** a real defence-in-depth layer. A task in a private subnet cannot be
reached from the internet even if its security group is wrong. Now the security
group is the only thing between the internet and the task.

**Say that second paragraph out loud.** Presenting this as a free optimisation
earns the follow-up *"what breaks if someone opens that SG?"* — and the answer
is the whole point: with NAT, a misconfigured SG is a latent bug; without it, a
misconfigured SG is an exposed container.

### Why RRF instead of a weighted score blend?

BM25 scores are unbounded and corpus-dependent; cosine similarity is bounded
[-1, 1]. There is no principled constant that makes `0.3 * bm25 + 0.7 * cosine`
mean anything, and whichever constant you pick is overfit to today's corpus.

RRF discards magnitudes and fuses **ranks**: `1/(k + rank)`, `k = 60`. Nothing
to tune per corpus.

**What RRF cannot do** — the question that separates people who read the paper
from people who used it: RRF cannot express *confidence*. A document ranked
first by both retrievers with an overwhelming margin scores identically to one
that barely won both. Fusing ranks discards exactly the signal you would want
for a "should I answer at all?" decision — which is why abstention is a separate
stage rather than a similarity threshold.

### Why did adding agents make it cheaper?

Counter-intuitive, so have it ready. The original pipeline was three full-model
calls. The six-role graph is one full-model call plus two cheap-model calls,
with reranking moved from a full model burning 2–3k tokens per query to a hosted
cross-encoder, and query rewriting folded into the router.

**Net: 3 × full → 1 × full + 2 × cheap.** More roles, less money.

The lesson: "number of agents" is not a cost unit. Tokens through expensive
models is.

### Where do migrations belong in a deploy pipeline?

Three candidate homes, two wrong here:

| where | verdict |
|---|---|
| API container startup | **Wrong.** N tasks race N migration runs against one schema. |
| GitHub runner, before deploy | **Impossible.** RDS is private; the runner cannot reach it. |
| One-shot ECS task the deploy waits on | **Correct.** In-VPC, runs once, exit code gates the deploy. |

The interesting part is that the second option is the one most people reach for,
and it fails for a reason with nothing to do with migrations: network topology.
A non-zero exit stops the deploy rather than shipping code against a mismatched
schema.

### Why `ignore_changes` on `desired_count` and `task_definition`?

Because CI/CD owns both. Autoscaling changes `desired_count`; the deploy
workflow changes `task_definition`. Without `ignore_changes`, every
`terraform apply` reverts the running service to whatever the last commit said —
silently rolling back a deploy as a side effect of an unrelated infra change.

**The general principle**, which is the actual answer: *Terraform owns the shape
of the infrastructure; the pipeline owns what is running inside it.* Any field
both parties write is a field that will flap.

### Why scopes split by consequence rather than by endpoint?

`ask` spends model budget. `index` mutates the corpus. `read` exposes other
people's questions. The eval harness needs the first and must not have the
second — a per-endpoint split cannot express that without enumerating routes
every time one is added.

The org is a property of the **key**, not a header. With auth on, `X-Org-Id` is
ignored entirely; otherwise any valid credential could name another tenant and
read its corpus.

**Follow-up:** *"Why 404 rather than 403 for another tenant's job?"* Because 403
confirms the resource exists. A job id must not be an oracle for another
tenant's work.

### Why does the UI keep its key in `sessionStorage`?

**The constraint:** it is a static export. Anything in `NEXT_PUBLIC_*` is inlined
into a bundle any visitor can read — a key put there is a published key.

**So:** the operator pastes a key into a header field, and it lives in
`sessionStorage` for that tab only.

**What that is not:** identity. Everyone sharing the deployment shares whatever
key they were handed, and the audit trail attributes to the key, not the person.
Per-user identity needs an authenticating proxy or an IdP — at which point
`authenticate()` is the single function that changes.

### How do you rate limit a service that autoscales?

**The trap:** an in-process counter. With N tasks behind an ALB, each task
enforces the limit independently, so the effective limit is N x the configured
one — and it *rises when you scale out*, which is precisely when you wanted it
to hold.

**So the counter has to be shared.** Redis here, because the stack already has
it for the semantic cache. `INCR` + `EXPIRE` on a key named for the window is
atomic and one round trip.

**Fixed window over sliding log**, deliberately. A sliding log is more accurate
at the boundary but stores one entry per request per key; a fixed window is two
integers. The cost is that a client can burst up to 2x the limit across an
instant spanning two windows — irrelevant at tens per minute, and not worth a
sorted set in the request path.

**Fail open, not closed.** If Redis is unreachable the limiter allows the
request and logs it. Refusing traffic because the *counter store* broke converts
a cost control into an availability incident, which is the wrong trade when the
thing being protected is a budget rather than a safety property.

**Per credential, not per IP.** An IP is not the unit that costs money: one key
behind a NAT is one payer, ten browsers sharing a key are still one payer, and
the machine clients this API mostly serves would defeat per-IP limiting anyway.

### Where does your Terraform state live, and what breaks if it doesn't?

**Local state means:** no locking, so two applies interleave and the loser's
resources are orphaned — created in AWS, absent from state, invisible to every
future plan. No history, so a partial write has no earlier version to roll back
to. And one laptop owns the stack.

**The fix has a chicken-and-egg:** the bucket holding the state cannot be
declared in the stack whose state it holds. Hence a separate `bootstrap/` root
module with local state that creates only the bucket.

**Why local state is acceptable *there*:** everything in bootstrap is
`prevent_destroy` or trivially re-importable, so losing that state file costs an
import, not an outage. Naming that asymmetry is the actual answer — otherwise it
looks like you applied the rule inconsistently.

**Locking without DynamoDB:** `use_lockfile` uses S3 conditional writes
(Terraform 1.10+). One less resource than the pattern most tutorials still show.

### You turn on a type checker and it finds 29 errors. Now what?

Three options, and the choice says more than the errors do.

**`|| true`** — the step runs, always passes, and everyone believes the codebase
is type-checked. This is strictly worse than not having it: it consumes the
attention that would otherwise notice the gap.

**Fix all 29 first** — sounds principled, but most of these are third-party
interface friction (an `**kwargs` overload, redis's `bytes | str`, langchain's
union content). Fixing them means changing how those libraries are called, which
is a real change that deserves its own review — not a rider on "turn on mypy."

**Grandfather the dirty modules, with counts.** Every other module is genuinely
checked, so a new file or a regression in a clean module fails CI. The debt is a
list in `pyproject.toml` with an error count per module, which means it is
countable, assignable, and visibly shrinking or not.

The general principle: **a gate that cannot fail is not a gate**, but a gate you
cannot turn on today is not one either. Scope it to what you can enforce now and
make the exceptions legible.

---

## Part 2 — Scenarios

Each of these actually happened in this repo.

### "Every commit to main deployed to production. Why is that bad, and what do you change?"

It made **deploying indistinguishable from saving work**. There was no way to
push a work-in-progress commit for CI to check without also building an image,
running migrations against production, and rolling the service.

**Fix:** `main` runs CI; `master` deploys. Reaching production takes an explicit
merge, and CI also runs on PRs *into* `master`, so the release is tested against
the merge result rather than only against `main` beforehand.

**The part people miss:** the workflow change alone protects nothing. Without a
branch ruleset requiring a PR, anyone can still `git push origin master` straight
past it. The YAML expresses intent; the ruleset enforces it.

### "You changed a secret in Terraform, applied successfully, and the app still rejects the new credential. Debug it."

The right answer is a question: **when did the running task start?**

ECS injects SSM parameters into the container **at start**, not on a schedule.
If the deployment predates the parameter update, the running task still holds the
old value in memory. The apply changed the parameter; it did not change the
process.

```
09:05  deployment created
12:44  SSM parameter updated      ← apply succeeded
       running task still holds the 09:05 value
```

**Fix:** force a new deployment.

**Generalisation:** "key rotation is a redeploy" is a property of injecting
secrets at container start — the price of keeping them out of the image and out
of the task definition.

### "A config value is malformed. Should the service start?"

**No — crash-loop deliberately.** `AUTH_API_KEYS` is parsed eagerly in the app
lifespan, so a malformed value fails at startup rather than on the first request
that happens to need a key.

The reasoning: an API that bills per call must not serve traffic it cannot
attribute. A task that starts anyway and 401s everything looks *broken*; a task
that refuses to start looks *misconfigured*, and only the second tells you where
to look. It also never reaches a healthy target group, so a bad config fails the
deploy instead of taking the service down.

**The trade-off:** eager validation turns a config typo into a failed deploy.
That is the correct direction, but it means the validation itself has to be
right — a false positive here is an outage.

### "The dashboard is deployed and every request 401s. The API is healthy."

Real bug, and the shape is worth recognising: **`setApiKey` was exported and
never called.** No field, no prompt, and the 401 handler did not offer one. The
only way to authenticate was to open devtools and write `sessionStorage` by hand.

Nothing was broken in a way a test would catch. The function worked. The API
worked. The wiring between them did not exist, and no unit test asserts that a UI
affordance exists.

**Takeaway:** an exported function with no caller is either dead code or a
missing feature, and from inside the module those are indistinguishable.

### "Your branch ruleset says 'does not target any resources'."

It was created without a target-branch condition, and rulesets default to
**Disabled** enforcement. Both fail silently — the ruleset exists, appears in the
list, and protects nothing.

Worth naming as a class: **a security control that fails open is worse than an
absent one**, because it consumes the attention that would otherwise go to
noticing the gap. You verify by attempting the thing that should fail:

```
! [remote rejected] master -> master (push declined due to repository rule violations)
```

### "A runbook step doesn't work when someone follows it exactly."

Two independent bugs in four lines:

1. The doc said `https://<api_cdn_url>`, but that Terraform output **already
   includes the scheme**. Following it literally produces `https://https://...`
   and a "could not resolve host: https" that looks nothing like a typo in a doc.
2. The example used `\"` escapes in PowerShell, which does not honour them when
   handing arguments to a native exe. The JSON arrived as a second URL and curl
   read `{...}` as glob syntax.

**The general point:** a runbook only ever read by its author is untested code.
Both bugs survived because the author already knew the answer and never ran their
own instructions from a clean shell.

---

## Part 3 — Short questions with traps

**"Your nDCG is 1.0 on every request. Why does that worry you?"**
A metric that never varies is measuring something other than what you think. It
is almost certainly ranking a single-item list.

**"`precision@k` is 0.225 but `hit_rate` is 0.906. Which is broken?"**
Neither. The right chunk is nearly always retrieved; most of what comes back with
it is not relevant. That is the expected shape for RAG with a generous `k`, and
precision is the wrong headline metric for a generator that can ignore noise.

**"Why catch `BaseException` in the job runner?"**
Normally wrong; right here. Cancellation and shutdown derive from it, and a job
table that lies about its own state is worse than no job table. The row must be
marked failed even while the process is being torn down.

**"A CloudWatch metric reads flat and low. What are the two readings?"**
Idle, or not sampled. Averaged gauges cannot distinguish them — which is why that
shape of graph is not evidence of anything on its own.

**"What is exit code 137?"**
SIGKILL. On Fargate, out of memory. The fix is a correctly sized task, not a
larger service.

**"What is the largest cost optimisation in this stack?"**
`terraform destroy` between demos. A stack that is not running costs nothing and
this one rebuilds in about fifteen minutes. Every other number in the cost table
is rounding error next to not running it.

---

## Related

- [`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md) — retrieval, agents, evaluation, 32 war stories
- [`AWS_AND_TERRAFORM.md`](AWS_AND_TERRAFORM.md) — infrastructure, cost, IAM, IaC
- [`DEPLOY_RUNBOOK.md`](DEPLOY_RUNBOOK.md) — the operational path these stories came from
