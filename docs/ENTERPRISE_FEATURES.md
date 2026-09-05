# LangSmith + Redis semantic cache + Multimodal RAG v1

## 1. Redis (Docker local)

```powershell
cd D:\Deeplearning\FinanceRAG
docker compose up -d redis
```

`.env`:
```env
REDIS_URL=redis://localhost:6379/0
CACHE_ENABLED=true
CACHE_TTL_SECONDS=3600
CACHE_SEMANTIC_THRESHOLD=0.92
```

Behavior:
- Exact match cache on normalized query
- Semantic cache via cosine similarity on query embeddings (≥ threshold)
- `/v1/index` and `/v1/upload` bump `corpus_version` and reset semantic index membership

Response fields: `cache_hit`, `cache_layer` (`exact` | `semantic`).

Later on AWS: point `REDIS_URL` at ElastiCache in the private VPC (same ECS task env).

## 2. LangSmith (local → ECS)

1. Use your regional UI — for APAC: [apac.smith.langchain.com](https://apac.smith.langchain.com) — create an API key there
2. `.env`:
```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=source-advisors-finance-rag
LANGSMITH_ENDPOINT=https://apac.api.smith.langchain.com
```
3. Restart API / ask once → traces appear under the project.

**Important:** UI at `apac.smith.langchain.com` requires API `https://apac.api.smith.langchain.com`. The US default (`api.smith.langchain.com`) rejects APAC keys with **403 Forbidden**. See [LangSmith cloud regions](https://docs.langchain.com/langsmith/cloud).

On ECS: set the same env vars (prefer SSM SecureString for `LANGSMITH_API_KEY`). Tracing is outbound HTTPS to LangSmith SaaS — no VPC peering required. Confirm compliance before enabling on production tax content.

## 3. Multimodal RAG v1 (caption path)

Ingest:
- Image files (`.png/.jpg/...`) → GPT-4o caption → text chunk (`modality=image`) → embed → Postgres/pgvector
- PDF page images extracted and captioned (capped by `MAX_IMAGES_PER_DOC`)
- Media saved under `data/media/<doc_id>/`

Ask:
- JSON: `POST /v1/ask` with optional `image_base64` + `image_mime`
- Multipart: `POST /v1/ask/multipart` with `query` + optional `image` file

Retrieval uses caption text like any other chunk; answers can cite image chunks.

## Quick verify

```powershell
docker compose up -d redis
pip install -e ".[dev]"
# set LANGSMITH_* if desired
uvicorn finance_rag.api.app:app --reload --port 8000
curl http://localhost:8000/health
```

Health includes `cache_enabled`, `langsmith`, `multimodal`.
