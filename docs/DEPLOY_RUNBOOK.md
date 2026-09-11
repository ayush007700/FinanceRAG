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

Four repository **variables** — not secrets; a browser doing the login must
know all of them:

```bash
terraform output -raw oidc_issuer      # → OIDC_ISSUER
terraform output -raw oidc_client_id   # → OIDC_CLIENT_ID
terraform output -raw oidc_token       # → OIDC_TOKEN   (always "id" for Cognito)
```

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

Cognito emails a temporary password. First sign-in prompts for a new one.

**Without `custom:org_id` the login succeeds and the API rejects the token** —
"token carries no tenant". That is deliberate: an unattributable person landing
in the default tenant is exactly what the tenancy column exists to prevent.

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
