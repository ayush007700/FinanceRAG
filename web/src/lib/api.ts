export type Citation = {
  chunk_id: string;
  doc_id: string;
  title: string;
  source: string;
  excerpt: string;
  score: number;
};

export type AskResponse = {
  answer: string;
  citations: Citation[];
  confidence: number;
  metrics: {
    latency_ms?: number | null;
    num_retrieved?: number;
    avg_relevance?: number | null;
    hit_rate?: number | null;
  };
  guardrails: string[];
  refused: boolean;
  trace_id: string | null;
  cache_hit?: boolean;
  cache_layer?: string | null;
};

export type HealthResponse = {
  status: string;
  service: string;
  company: string;
  cache_enabled?: boolean;
  langsmith?: boolean;
  multimodal?: boolean;
};

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function readError(res: Response): Promise<string> {
  try {
    const data = await res.json();
    if (typeof data?.detail === "string") return data.detail;
    return JSON.stringify(data);
  } catch {
    return res.statusText || "Request failed";
  }
}

export async function getHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_URL}/health`, { cache: "no-store" });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function askQuestion(
  query: string,
  serviceLine?: string
): Promise<AskResponse> {
  const res = await fetch(`${API_URL}/v1/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      service_line: serviceLine || null,
    }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function indexCorpus(paths: string[] = ["data/corpus"]) {
  const res = await fetch(`${API_URL}/v1/index`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

export async function uploadFile(file: File) {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${API_URL}/v1/upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}
