# Deploy runbook

From nothing to a working deployment, and back to nothing.

`terraform apply` on its own gives you **empty infrastructure**: no image in
ECR, no schema in RDS, no documents indexed. Four steps, in this order, because
each depends on the one before.

```
apply  ──▶  update GitHub config  ──▶  push  ──▶  index
 15 min          5 min                 8 min      5 min
```

---

## Step 0 — before you start

`infra/terraform/terraform.tfvars` must exist and be complete. It is gitignored,
so it does not survive a fresh clone.

```hcl
aws_region   = "ap-south-1"
project_name = "source-advisors-finance-rag"

openai_api_key = "sk-proj-..."
cohere_api_key = "..."                 # empty disables reranking
db_password    = "..."                 # RDS master password

# API credentials: key_id:org_id:scopes:secret, comma-separated.
# Scopes are |-separated from ask, index, read, metrics, or * for all.
auth_api_keys = "demo:default:*:<40-char-secret>"
auth_enabled  = true

alarm_email     = "you@example.com"    # SNS emails a confirmation to click
container_image = ""                   # empty on first apply; CD sets it after
github_org_repo = "youruser/FinanceRAG"
```

Generate the two secrets (no OpenSSL needed on Windows):

```powershell
# RDS password — URL-unreserved characters only, since it is embedded in DATABASE_URL
python -c "import secrets,string; a=string.ascii_letters+string.digits+'-_.~'; print(''.join(secrets.choice(a) for _ in range(28)))"

# API key secret
python -c "import secrets,string; a=string.ascii_letters+string.digits+'-_'; print(''.join(secrets.choice(a) for _ in range(40)))"
```

> **`auth_api_keys` is not optional.** `AUTH_ENABLED` defaults on outside
> development, and a deployed task with auth on and no keys **refuses to start**
> rather than serving `/v1` openly. Leaving it blank produces a deploy that
> fails health checks with no obvious cause.

> **All four fields are required.** The generator above prints only the *secret*,
> and pasting it alone is the most common mistake here:
>
> ```hcl
> auth_api_keys = "<40-char-secret>"                  # WRONG - bare secret
> auth_api_keys = "demo:default:*:<40-char-secret>"   # RIGHT - key_id:org_id:scopes:secret
> ```
>
> A bare secret raises `AUTH_API_KEYS entry 1 is malformed` at parse time. The
> bearer token you send is the **secret field only** — never the whole string.

---

## Step 1 — apply

```bash
cd infra/terraform
terraform init
terraform apply
```

~15 minutes; RDS and the two CloudFront distributions dominate. Creates ~70
resources: VPC, ALB, ECS cluster, RDS + pgvector, ECR, S3 (uploads + UI),
CloudFront for both, SSM parameters, CloudWatch alarms and SNS.

**Expected afterwards:** the ECS service is *unhealthy*. It is running the ECR
`:latest` placeholder, and no image exists until CD pushes one. That is not a
fault.

---

## Step 2 — update GitHub config

Every value below **changes on every recreate**. This is the step people forget,
and the failure is confusing: CD authenticates against a deleted IAM user, or
the UI publishes to a bucket that no longer exists.

### 2a. Read the values

```bash
cd infra/terraform

terraform output -raw github_actions_access_key_id
terraform output -raw github_actions_secret_access_key
terraform output -raw api_cdn_url
terraform output -raw ui_bucket
terraform output -raw ui_distribution_id
```

`terraform output` alone lists everything; `-raw` prints one value unquoted,
which is what you want for copy-paste.

> The secret access key is marked `sensitive`, so plain `terraform output` masks
> it. `-raw` is the only way to read it. It exists in `terraform.tfstate` too —
> which is precisely why that file is gitignored.

### 2b. Secrets (encrypted, masked in logs)

**`https://github.com/<you>/<repo>/settings/secrets/actions`**

Or: repo → **Settings** → sidebar **Secrets and variables** → **Actions** →
**Secrets** tab → **New repository secret**.

| name | from |
|---|---|
| `AWS_ACCESS_KEY_ID` | `terraform output -raw github_actions_access_key_id` |
| `AWS_SECRET_ACCESS_KEY` | `terraform output -raw github_actions_secret_access_key` |

### 2c. Variables (plain text, visible in logs)

**`https://github.com/<you>/<repo>/settings/variables/actions`**

Same page, **Variables** tab → **New repository variable**. This is the tab
people miss — variables and secrets look identical in the sidebar.

| name | from |
|---|---|
| `API_BASE_URL` | `terraform output -raw api_cdn_url` |
| `UI_BUCKET` | `terraform output -raw ui_bucket` |
| `UI_DISTRIBUTION_ID` | `terraform output -raw ui_distribution_id` |

**Why variables rather than secrets:** none are sensitive, and secrets are
masked in logs — a masked bucket name makes a failed `s3 sync` painful to debug.
`API_BASE_URL` is also inlined into the JavaScript bundle at build time
(`NEXT_PUBLIC_*`), so it is public by construction; storing it as a secret would
imply a protection that does not exist.

---

## Step 3 — push

`main` is the integration branch and `master` is the release branch. Pushing to
`main` runs CI only; **deploys happen when `main` merges into `master`.**

```bash
git push origin main                      # CI: ruff + pytest
gh pr create --base master --head main    # then merge it -> CD deploys
```

Or, to redeploy the current `master` with nothing to commit: **Actions** → **CD**
→ **Run workflow** (`workflow_dispatch`), or latest run → **Re-run all jobs**.

CD runs **Build → Migrate → Deploy**, plus an independent UI job:

| step | what |
|---|---|
| Build | `docker build` → ECR, tagged with the commit SHA |
| **Migrate** | one-shot ECS task, `alembic upgrade head`, **inside the VPC** |
| Deploy | render task definition with the new image → ECS rolling update |
| UI | `next build` static export → S3 sync → CloudFront invalidation |

Migrations run inside the VPC because **RDS is private and unreachable from a
GitHub runner**, and not on API startup because N tasks would race N migration
runs against one schema. A non-zero exit stops the deploy rather than shipping
code against a mismatched schema.

Verify:

```bash
curl.exe <api_cdn_url>/health
```

`{"status":"ok","database":true}` means the image is live and RDS is reachable.

---

## Step 4 — index the corpus

RDS now has the schema and **no documents**. Every question would correctly
refuse, because there is nothing to retrieve.

Use `Invoke-RestMethod`, not `curl.exe`. PowerShell 5.1 does not honour `\"`
as an escape and re-splits the result when handing arguments to a native exe, so
`-d "{\"paths\":...}"` arrives as a second URL and curl reads the `{...}` as
glob syntax — `curl: (3) bad range specification`.

```powershell
$api = "<api_cdn_url>"       # already includes https:// — do not add it again
$key = "<secret field from auth_api_keys>"
$h   = @{ Authorization = "Bearer $key" }

$job = Invoke-RestMethod -Method Post -Uri "$api/v1/index" -Headers $h `
  -ContentType "application/json" `
  -Body (@{ paths = @("data/corpus") } | ConvertTo-Json)
$job
```

Returns `202` with a job id. Poll it:

```powershell
Invoke-RestMethod -Uri "$api/v1/jobs/$($job.job_id)" -Headers $h
```

If you must use `curl.exe`, put the body in a file and pass `-d "@body.json"`;
inline JSON is not worth the quoting fight.

`succeeded` with ~972 chunks takes about 4 minutes. The work runs as its own
ECS task at 2 vCPU / 8 GiB — it cannot run inside the API container, which is
sized at 0.5 vCPU / 1 GiB and gets SIGKILLed (exit 137) parsing a 113-page PDF.

Then ask something real:

```powershell
$q = @{ query = "What is the four-part test for R&D tax credit qualification?" }
Invoke-RestMethod -Method Post -Uri "$api/v1/ask" -Headers $h `
  -ContentType "application/json" -Body ($q | ConvertTo-Json)
```

---

## Step 5 — sign-in for people (optional)

Everything so far authenticates with API keys, which is right for machines and
wrong for people: a shared key means the audit trail cannot say who asked.
Turning on OIDC is one flag and one apply.

### 5a. Provision Cognito

```hcl
# terraform.tfvars
enable_cognito = true
```

```bash
cd infra/terraform && terraform apply
```

This creates a user pool, a public app client (no secret — PKCE stands in for
it), a hosted login page, and four groups named exactly after the API scopes:
`ask`, `index`, `read`, `metrics`. The API is wired to it in the same apply;
nothing in `auth_jwt` needs copying.

### 5b. Tell the UI build about it

Three repository **variables** — not secrets; a browser doing the login must
know all of them:

```bash
terraform output -raw oidc_issuer      # → OIDC_ISSUER
terraform output -raw oidc_client_id   # → OIDC_CLIENT_ID
terraform output -raw oidc_token       # → OIDC_TOKEN   (always "id" for Cognito)
```

A fourth, `OIDC_SCOPES`, defaults to `openid profile email` and only needs
setting for a provider that names its scopes differently. Cognito does not.

**`https://github.com/<you>/<repo>/settings/variables/actions`**, then push to
`main` and merge to `master` so CD rebuilds the bundle with them. The Sign in
button appears once the new bundle is live.

> `OIDC_TOKEN=id` is a Cognito property, not a preference. Its access tokens
> carry `client_id` rather than `aud` and omit custom attributes; the ID token
> has both, and the API pins audience. Auth0 and Entra do not need this.

### 5c. Create the first user

Self-service sign-up is off — this is an internal tool, and an account is
something an admin creates for a named person in a named tenant.

```bash
POOL=$(terraform output -raw cognito_user_pool_id)

aws cognito-idp admin-create-user   --user-pool-id "$POOL"   --username you@example.com   --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true                     Name=custom:org_id,Value=default

# Group membership is what becomes scopes. `ask` and `read` for a person who
# questions the corpus; add `index` only for someone who may rewrite it.
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username you@example.com --group-name ask
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username you@example.com --group-name read
```

The same from PowerShell (`` ` `` for continuation, not `\`):

```powershell
$POOL  = terraform output -raw cognito_user_pool_id
$EMAIL = "you@example.com"

aws cognito-idp admin-create-user `
  --user-pool-id $POOL --username $EMAIL `
  --user-attributes Name=email,Value=$EMAIL Name=email_verified,Value=true Name=custom:org_id,Value=default

aws cognito-idp admin-add-user-to-group --user-pool-id $POOL --username $EMAIL --group-name ask
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL --username $EMAIL --group-name read

# Confirm: groups and tenant
aws cognito-idp admin-list-groups-for-user --user-pool-id $POOL --username $EMAIL --query "Groups[].GroupName"
```

Cognito emails a temporary password from `no-reply@verificationemail.com`. It
lands in spam more often than not; look there first. The user's status stays
`FORCE_CHANGE_PASSWORD` until first sign-in, which prompts for a new one.

**Without `custom:org_id` the login succeeds and the API rejects the token** —
"token carries no tenant". That is deliberate: an unattributable person landing
in the default tenant is exactly what the tenancy column exists to prevent.

**What the user sees.** With OIDC configured, the UI's front door is a sign-in
screen; the dashboard is behind it. **Sign in** → Cognito's hosted page (email,
temporary password, then a new password) → back to the dashboard with the
badge reading *Signed in: you@example.com*. An operator without an account
can expand *Have an API key instead?* on the same screen.

### 5d. Verify

Open the UI → **Sign in** → Cognito's hosted page → back to the dashboard with
the badge reading *Signed in: you@example.com*. Ask a question, then:

```bash
curl.exe <api_cdn_url>/v1/audit -H "Authorization: Bearer <an api key with read>"
```

The newest row's `user_id` is the Cognito `sub`, not a key id. That is the
whole point of the step.

### Bring your own IdP instead

Skip 5a. Set `auth_jwt` in tfvars with your issuer's JWKS URL, issuer,
audience and claim names, and the four `OIDC_*` variables from your provider.
An explicit `auth_jwt` always wins over Cognito.

---

## Metrics and logs

CloudWatch is the source of truth in AWS. Prometheus exists for local
`docker compose` and is inert in the deployment — nothing scrapes it.

### Logs

Everything the container writes to stdout goes to one log group through the
ECS `awslogs` driver. Structured JSON, one event per line.

```bash
export MSYS_NO_PATHCONV=1     # Git Bash mangles the leading slash otherwise
aws logs tail /ecs/source-advisors-finance-rag --since 30m --follow

# Everything except health checks
aws logs filter-log-events --log-group-name /ecs/source-advisors-finance-rag \
  --start-time $(( $(date +%s) - 1800 ))000 --filter-pattern '-"GET /health"' \
  --query "events[].message" --output text
```

Console: **CloudWatch → Log groups → `/ecs/source-advisors-finance-rag`**, then
*Logs Insights* for queries across time:

```
fields @timestamp, event, latency_ms, route
| filter event = "ask_completed"
| stats avg(latency_ms), pct(latency_ms, 95) by bin(5m)
```

### Metrics

Three sources, all in CloudWatch:

| namespace | what | who emits it |
|---|---|---|
| `AWS/ECS`, `AWS/ApplicationELB` | CPU, memory, 5xx, target response time | AWS, automatically |
| `ECS/ContainerInsights` | per-task CPU and memory | Container Insights |
| **`FinanceRAG/SourceAdvisors`** | `RequestLatencyMs`, `Requests`, `TopCosine`, `HallucinatedCitations`, `Refusals` | the app, one `PutMetricData` per request |

The last row is the one that says whether the *answers* are good, not just
whether the service is up. `TopCosine` dropping means retrieval is finding
less; `Refusals` rising means the answerability gate is saying no more often;
`HallucinatedCitations` above zero means the citation check caught something.

```bash
aws cloudwatch get-metric-statistics --namespace FinanceRAG/SourceAdvisors \
  --metric-name RequestLatencyMs --dimensions Name=Endpoint,Value=ask Name=Status,Value=ok \
  --start-time $(date -u -d '-1 hour' +%FT%TZ) --end-time $(date -u +%FT%TZ) \
  --period 300 --statistics Average p95
```

Console: **CloudWatch → Metrics → All metrics → `FinanceRAG/SourceAdvisors`**.
The dashboard (`terraform output -raw cloudwatch_dashboard`) has these
alongside the ECS and ALB panels.

### Alarms

Three, all publishing to the SNS topic your `alarm_email` subscribes to:
ALB 5xx rate, ECS CPU, and `RequestLatencyMs` p95.

**The subscription is inert until confirmed.** After the first apply, AWS
emails `alarm_email` from `no-reply@sns.amazonaws.com` with the subject
*AWS Notification - Subscription Confirmation*. It lands in spam more often
than not, and until the link in it is clicked the alarms fire into nothing.
Check:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn $(terraform output -raw alarm_topic_arn) \
  --query "Subscriptions[].[Endpoint,SubscriptionArn]" --output text
```

`PendingConfirmation` means not yet. If the email is gone, resubscribing sends
a fresh one:

```bash
aws sns subscribe --topic-arn $(terraform output -raw alarm_topic_arn) \
  --protocol email --notification-endpoint you@example.com
```

### What `/metrics` is for

The API exposes Prometheus text at `/metrics`, behind the `metrics` scope. In
AWS nothing scrapes it: the same five signals are pushed to CloudWatch, which
is what the dashboard and alarms read. `/metrics` and `infra/prometheus/` are
for running Prometheus locally against `docker compose`. Grafana is not part
of this stack; CloudWatch's dashboard is the viewer.

---

## Going to production

The defaults are a demo profile. `terraform.tfvars.prod.example` is the other
one, with the reason beside each setting. What each line buys, and what it
costs, in the order an outage would find them:

| setting | demo | prod | why it is required |
|---|---|---|---|
| `db_multi_az` | false | **true** | An AZ event or a maintenance reboot is downtime for every request. With a standby it is ~60s of failover. Doubles the RDS line. |
| `desired_count` | 1 | **2** | With one task, every rolling deploy has a moment with zero healthy targets. Two is the floor; autoscaling raises it. |
| `db_deletion_protection` | false | **true** | Two deliberate steps between an operator and the data, not one. |
| `db_skip_final_snapshot` | true | **false** | `destroy` keeps the data. In a demo the data is the corpus, which is in git. In production it is every audit row. |
| `db_backup_retention_days` | 7 | **14** | Point-in-time recovery window. Seven covers "noticed Monday, broke Friday"; fourteen covers a holiday. |
| `enable_waf` | false | **true** | Per-IP rate limiting and managed threat rules at the edge, *before* a task spends CPU rejecting a request. See below. |
| `enable_tracing` | false | **true** | A request becomes a timeline instead of one number. See below. |
| `acm_certificate_arn` | "" | set | TLS from CloudFront to the ALB. Needs a domain. See below. |

Rough delta: ~$41/month → ~$85/month. The NAT gateway stays off either way.

### WAF — why the application's rate limiter is not enough

The API limits requests per credential. That limiter runs *inside* the task:
a request has to reach Fargate, be authenticated, and increment a counter
before it can be refused. An unauthenticated flood -- credential stuffing on
`/v1/ask`, a bot on `/health` -- never meets that limiter and costs CPU on
every request it rejects.

WAF sits at the CloudFront edge. It rate-limits per IP before authentication,
and the AWS managed rule groups (IP reputation, OWASP common threats,
known-bad inputs) are maintained against new CVE classes without anyone here
writing a rule. It must be created in `us-east-1` regardless of region; that
is a CloudFront constraint, and `versions.tf` carries the aliased provider for
it.

```hcl
enable_waf = true
```

One managed rule is downgraded to *count*: the 8 KB body-size limit, because a
question with an attached image is larger than that by design. The API's own
input limit is the control there.

### TLS to the origin — why it needs a domain

CloudFront to the ALB is HTTP. Locked to the CloudFront prefix list and a
shared secret header, but plaintext across AWS's network. The fix is a
certificate on the ALB, and the ALB's default DNS name cannot present a valid
one -- only a name you control can.

1. Register or delegate a domain; create a hosted zone in Route 53.
2. **ACM → Request certificate** for `api.yourdomain.com` in `ap-south-1`,
   DNS validation, add the CNAME it gives you.
3. `acm_certificate_arn = "<arn>"` in tfvars, `terraform apply`. The ALB gains
   an HTTPS listener and CloudFront switches to `https-only` to the origin.
4. Route 53: `api.yourdomain.com` → the ALB. CloudFront's origin can then be
   that name rather than the ALB's default.

Until step 3, the `.trivyignore.yaml` entry for `AVD-AWS-0054` is the record
that this is known. Delete the entry when the cert lands.

### Tracing — why CloudWatch metrics are not enough

CloudWatch says a request took 160 seconds. It cannot say *where*. The 504 in
this repo's history spent 82 seconds waiting on one embeddings call, and the
only way to see that was reading log timestamps by hand.

With `enable_tracing = true` the ADOT collector runs as a sidecar in the API
task, the app exports OpenTelemetry spans to it on `localhost:4318`, and it
forwards them to X-Ray. Each pipeline stage -- supervisor, researcher,
answerability, analyst, critic, compliance -- is its own span, so a trace
reads as the pipeline with each bar as long as it took.

**X-Ray → Traces**, filter `service("source-advisors-finance-rag-api")`.

Two things that had to be true for spans to appear at all: X-Ray requires the
first four bytes of a trace id to be a timestamp and silently drops the rest,
so the app uses the AWS id generator; and the collector is `essential = false`
so a collector crash does not take the API down -- tracing is diagnostics.

---

## Disaster recovery

What can be lost, what protects it, and how long each takes to get back.

### What holds state

| store | contains | protection |
|---|---|---|
| **RDS** | corpus chunks + embeddings, audit trail, conversation memory, jobs, eval runs | automated backups, point-in-time recovery |
| **S3 uploads** | documents uploaded through `/v1/upload` | versioning |
| **S3 ui** | the built bundle | rebuilt by CD from git; nothing to protect |
| **SSM** | secrets | in Terraform; re-applied from tfvars |
| **Terraform state** | the record of everything above | versioned S3 bucket, 90-day version history |

Everything else -- ECS, ALB, CloudFront, Cognito's *configuration* -- is
`terraform apply` from nothing. Cognito *users* are not: see below.

### Objectives

| | demo profile | prod profile |
|---|---|---|
| **RPO** (data you can lose) | ≤ 5 min — PITR granularity | ≤ 5 min |
| **RTO** (time to serve again) | ~30 min — restore + apply + deploy | ~30 min; AZ failure alone: ~60 s via Multi-AZ |
| Backup retention | 7 days | 14 days |

The RTO is dominated by RDS restore (~10–15 min) and a CD run (~8 min). The
corpus can also be re-indexed from git in ~2 min, which is faster than a
restore if the audit trail is not needed.

### Restore RDS

Point-in-time, to a **new** instance. The original is left alone until the
restored one is confirmed good.

```bash
SRC=source-advisors-finance-rag-db
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier "$SRC" \
  --target-db-instance-identifier "$SRC-restored" \
  --restore-time 2026-09-11T10:00:00Z \
  --db-subnet-group-name $(aws rds describe-db-instances --db-instance-identifier "$SRC" \
      --query "DBInstances[0].DBSubnetGroup.DBSubnetGroupName" --output text) \
  --vpc-security-group-ids $(aws rds describe-db-instances --db-instance-identifier "$SRC" \
      --query "DBInstances[0].VpcSecurityGroups[0].VpcSecurityGroupId" --output text)

aws rds wait db-instance-available --db-instance-identifier "$SRC-restored"
```

Then point the stack at it. The endpoint is in the `DATABASE_URL` SSM
parameter Terraform manages, so the clean path is to make Terraform adopt the
restored instance rather than editing the parameter by hand:

```bash
cd infra/terraform
terraform state rm aws_db_instance.main
terraform import aws_db_instance.main "$SRC-restored"
terraform apply          # rewrites DATABASE_URL; plan shows only that
```

Then a deploy (merge to `master`, or **Actions → CD → Run workflow**) so the
tasks start with the new parameter. `curl <api_cdn_url>/health` →
`"database": true`, and an ask returns a cited answer.

### Restore an uploaded document

Versioning is on. A deleted or overwritten object is a version away:

```bash
aws s3api list-object-versions --bucket <uploads-bucket> --prefix default/ \
  --query "Versions[?IsLatest==\`false\`].[Key,VersionId,LastModified]" --output table
aws s3api copy-object --bucket <uploads-bucket> --key <key> \
  --copy-source "<uploads-bucket>/<key>?versionId=<id>"
```

### What is not backed up

- **Cognito users.** The pool's configuration is Terraform; its users are
  not, and there is no built-in export. For a handful of internal users,
  recreating them from Step 5c is the plan. Beyond that, a scheduled
  `list-users` to S3 is the cheapest backup.
- **Redis.** A cache. Losing it costs one cold request per query.

### The drill

A restore procedure nobody has run is a hypothesis. Quarterly, in the demo
profile where it costs a few cents:

1. Restore to `-restored` as above, from one hour ago.
2. `psql` into it from a task -- or simpler, point a throwaway `terraform
   workspace` at it -- and check `select count(*) from chunks` matches.
3. Delete the restored instance. Note the wall-clock time; that is the RTO
   you actually have, not the one in the table.

---

## Finding everything in AWS

`terraform output` is the source of truth. This section is for when you are in
the console, or state is unavailable.

Everything is tagged with `project_name` (`source-advisors-finance-rag`), so the
name prefix is the thread to pull on. **Set your region first** — all of it lives
in `ap-south-1`, and the console silently shows an empty list in the wrong one.
CloudFront is the exception: it is global, listed under "Global" regardless.

| what | console | CLI |
|---|---|---|
| **UI site** | CloudFront → distribution commented `... UI` → *Distribution domain name* | `terraform output -raw ui_url` |
| **API endpoint** | CloudFront → distribution commented `... API` | `terraform output -raw api_cdn_url` |
| UI bucket | S3 → `<project>-ui-<account-id>` | `terraform output -raw ui_bucket` |
| API containers | ECS → cluster `<project>-cluster` → service `<project>-svc` | `terraform output -raw ecs_service_name` |
| Images | ECR → repository `finance-rag` | `terraform output -raw ecr_repository_url` |
| Database | RDS → `<project>` (private; no public endpoint) | `terraform output -raw rds_endpoint` |
| Logs | CloudWatch → Log groups → `/ecs/<project>` | `terraform output -raw cloudwatch_log_group` |
| Secrets at runtime | Systems Manager → Parameter Store → `/<project>/*` | `aws ssm get-parameters-by-path --path /<project> --with-decryption` |
| Metrics | CloudWatch → Dashboards | `terraform output -raw cloudwatch_dashboard` |

The two CloudFront distributions are the pair people confuse. Both are
`d*.cloudfront.net` and neither name says which is which — read the **Comment**
column, or the **Origin**: the UI points at an S3 bucket, the API at the ALB.

```bash
aws cloudfront list-distributions   --query "DistributionList.Items[].{Id:Id,Domain:DomainName,Comment:Comment,Origin:Origins.Items[0].DomainName}"   --output table
```

Note that Parameter Store, not `terraform.tfvars`, is what the running task
reads. Editing tfvars changes nothing until `terraform apply` writes it through
— so when a credential works locally but not against the deployment, compare
those two before touching anything else.

---

## Pausing and resuming

The stack rebuilds from nothing in about fifteen minutes, so between working
sessions the cheapest state is *not running*. This is the checklist for
leaving and coming back without losing anything that matters.

### Before `destroy`

1. **Snapshot the database** if there is anything in it you want back. The
   demo profile skips the final snapshot, so without this the audit rows and
   conversations go with the instance. A manual snapshot of a 20 GB
   `t4g.micro` costs about a cent a month.

   ```bash
   SNAP="finance-rag-pause-$(date +%Y%m%d)"
   aws rds create-db-snapshot --db-instance-identifier source-advisors-finance-rag-db      --db-snapshot-identifier "$SNAP"
   aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAP"
   ```

2. **Keep `terraform.tfvars`.** It is gitignored, it holds every secret, and
   it is the only copy. Back it up somewhere that is not this directory.

3. Nothing else needs saving. Code is in git, state is in S3, the corpus is
   in git and re-indexes in two minutes.

### `destroy`

```bash
cd infra/terraform
terraform destroy      # ~15 min; CloudFront is most of it
```

### What is gone, and what is not

| gone | comes back how |
|---|---|
| RDS and everything in it | restore the snapshot (below), or re-index from git |
| Cognito pool and its users | `enable_cognito = true` recreates the pool; users are recreated per Step 5c |
| CloudFront distributions, ALB, IAM user, WAF | recreated with **new identifiers** -- see "on return" |
| SSM parameters | rewritten from tfvars on apply |
| ECR images | CD pushes a new one on the first deploy |

| **not gone** | why |
|---|---|
| Terraform state bucket | separate bootstrap module, `prevent_destroy`; costs ~$0 |
| Manual RDS snapshots | not managed by Terraform; delete by hand when no longer wanted |
| GitHub secrets and variables | still set, and **now stale** -- see below |

### On return

Every step in this runbook's Steps 1–5, in order, because every identifier
changes on recreate:

1. `terraform apply` -- ~15 min.
2. **Step 2 again, completely.** The IAM access key, both CloudFront ids, the
   UI bucket, and (with Cognito) the issuer and client id are all new. The
   GitHub secrets and variables from last time point at resources that no
   longer exist, and the failure mode is a CD run that authenticates against
   a deleted user.
3. Merge to `master` for the first deploy.
4. Index the corpus (Step 4), or restore the snapshot:

   ```bash
   # Restore to a new instance, then make Terraform adopt it -- see
   # "Disaster recovery" for the full sequence.
   aws rds restore-db-instance-from-db-snapshot      --db-instance-identifier source-advisors-finance-rag-db-restored      --db-snapshot-identifier finance-rag-pause-YYYYMMDD ...
   ```

   Re-indexing is faster than restoring unless the audit trail matters.
5. Recreate the Cognito user (Step 5c) and confirm the SNS email again.

### Confirm nothing is still billing

```bash
aws s3api list-buckets --query "Buckets[?starts_with(Name,'source-advisors')].Name"
aws cloudfront list-distributions --query "DistributionList.Items[].[Id,Enabled,Comment]" --output table
aws rds describe-db-instances --query "DBInstances[].DBInstanceIdentifier"
aws rds describe-db-snapshots --snapshot-type manual --query "DBSnapshots[].DBSnapshotIdentifier"
```

The state bucket and any manual snapshots are the expected survivors. A
CloudFront distribution still `Enabled` fifteen minutes later means the
destroy did not finish; run it again.

---

## Tearing down

```bash
cd infra/terraform
terraform destroy
```

CloudFront takes ~15 minutes to disable and delete, so `destroy` may return
before the distributions are fully gone. Afterwards, confirm nothing is left
billing:

```bash
aws s3api list-buckets --query "Buckets[?starts_with(Name,'source-advisors')].Name"
aws cloudfront list-distributions --query "DistributionList.Items[].[Id,Enabled,Comment]"
```

A bucket with objects and `force_destroy = false` blocks its own deletion and is
the usual leftover.

---

## Gotchas

**Check your CLI region first.** `aws configure get region`. Querying the wrong
region reports "does not exist" for infrastructure that is running — maximally
misleading while debugging.

**Git Bash mangles leading slashes.** `/ecs/log-group` becomes a Windows path.
Use `export MSYS_NO_PATHCONV=1` for any AWS CLI argument starting with `/`.

**Never pipe `terraform apply` through `tail` or `grep`.** A shell pipeline
reports the *last* command's exit code, so a failed apply looks like a success.
Redirect to a file instead: `terraform apply > apply.log 2>&1`.

**`curl` in PowerShell is `Invoke-WebRequest`.** Use `curl.exe`, and `` ` `` for
line continuation rather than `\`.

**Don't push while indexing.** A deploy replaces the API container. That no
longer kills the job — indexing runs as its own task — but it does interrupt any
in-flight request.
