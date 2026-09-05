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

// The /v1 API requires a bearer credential. It deliberately does NOT come from
// NEXT_PUBLIC_*: this is a static export, so anything in the environment is
// inlined into a bundle any visitor can read, and a published key is not a key.
// The operator supplies their own instead, which stays in this tab only.
const API_KEY_STORAGE = "finance_rag_api_key";

export function setApiKey(key: string): void {
  sessionStorage.setItem(API_KEY_STORAGE, key);
}

function authHeaders(): Record<string, string> {
  // Guarded for the server-rendering pass, where sessionStorage does not exist.
  const key =
    typeof window === "undefined"
      ? null
      : window.sessionStorage.getItem(API_KEY_STORAGE);
  return key ? { Authorization: `Bearer ${key}` } : {};
}

async function readError(res: Response): Promise<string> {
  // 401 carries no useful server detail ("missing bearer credential"); 403 does
  // -- it names the scope the key lacks -- so that one falls through below.
  if (res.status === 401) {
    return "Not authenticated: set an API key for this session.";
  }
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
    headers: { "Content-Type": "application/json", ...authHeaders() },
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
    headers: { "Content-Type": "application/json", ...authHeaders() },
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
    headers: authHeaders(),
    body: form,
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}
