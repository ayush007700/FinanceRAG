# FinanceRAG Web Dashboard

Simple Next.js UI for Source Advisors FinanceRAG.

## Run

1. Start the API (`uvicorn finance_rag.api.app:app --reload --port 8000`)
2. In this folder:

```powershell
cd web
copy .env.local.example .env.local
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

## Config

`NEXT_PUBLIC_API_URL` — FastAPI base URL (default `http://localhost:8000`)

For the deployed ALB:

```env
NEXT_PUBLIC_API_URL=http://source-advisors-finance-rag-alb-999162450.ap-south-1.elb.amazonaws.com
```

## Features

- Ask questions with service-line filter
- Show answer, citations, confidence, latency, cache hit
- Re-index corpus
- Upload a document
- API health badges (Redis / multimodal / LangSmith)
