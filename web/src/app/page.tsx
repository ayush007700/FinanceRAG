"use client";

import { FormEvent, useEffect, useState } from "react";
import {
  AskResponse,
  HealthResponse,
  askQuestion,
  getHealth,
  indexCorpus,
  uploadFile,
} from "@/lib/api";
import styles from "./page.module.css";

const EXAMPLES = [
  "What values guide Source Advisors?",
  "What is the four-part test for R&D credits?",
  "How does cost segregation improve cash flow?",
];

export default function HomePage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [query, setQuery] = useState(EXAMPLES[0]);
  const [serviceLine, setServiceLine] = useState("");
  const [loading, setLoading] = useState(false);
  const [indexing, setIndexing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AskResponse | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch(() =>
        setHealth({
          status: "down",
          service: "finance-rag",
          company: "Source Advisors",
        })
      );
  }, []);

  async function onAsk(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setStatusMsg(null);
    try {
      const data = await askQuestion(query.trim(), serviceLine || undefined);
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ask failed");
    } finally {
      setLoading(false);
    }
  }

  async function onIndex() {
    setIndexing(true);
    setError(null);
    try {
      const data = await indexCorpus();
      setStatusMsg(`Indexed ${data.chunks ?? "?"} chunks`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Index failed");
    } finally {
      setIndexing(false);
    }
  }

  async function onUpload(file: File | null) {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const data = await uploadFile(file);
      setStatusMsg(`Uploaded and indexed: ${data.path}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  const apiUp = health?.status === "ok";

  return (
    <main className={styles.shell}>
      <header className={styles.hero}>
        <p className={styles.eyebrow}>Internal tools</p>
        <h1 className={styles.brand}>Source Advisors</h1>
        <p className={styles.sub}>
          FinanceRAG dashboard — ask grounded questions across R&amp;D, cost
          segregation, energy incentives, and more.
        </p>
        <div className={styles.badges}>
          <span className={apiUp ? styles.badgeOk : styles.badgeDown}>
            API {apiUp ? "online" : "offline"}
          </span>
          {health?.cache_enabled ? (
            <span className={styles.badge}>Redis cache</span>
          ) : null}
          {health?.multimodal ? (
            <span className={styles.badge}>Multimodal</span>
          ) : null}
          {health?.langsmith ? (
            <span className={styles.badge}>LangSmith</span>
          ) : null}
        </div>
      </header>

      <div className={styles.grid}>
        <section className={styles.panel}>
          <h2>Ask</h2>
          <form onSubmit={onAsk} className={styles.form}>
            <label className={styles.label}>
              Question
              <textarea
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                rows={4}
                required
                minLength={3}
              />
            </label>
            <label className={styles.label}>
              Service line (optional)
              <input
                value={serviceLine}
                onChange={(e) => setServiceLine(e.target.value)}
                placeholder="R&D Tax Credit"
              />
            </label>
            <div className={styles.examples}>
              {EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  type="button"
                  className={styles.chip}
                  onClick={() => setQuery(ex)}
                >
                  {ex}
                </button>
              ))}
            </div>
            <button className={styles.primary} disabled={loading || !apiUp}>
              {loading ? "Thinking…" : "Ask FinanceRAG"}
            </button>
          </form>

          <div className={styles.tools}>
            <h3>Knowledge tools</h3>
            <button
              type="button"
              className={styles.secondary}
              onClick={onIndex}
              disabled={indexing || !apiUp}
            >
              {indexing ? "Indexing…" : "Re-index data/corpus"}
            </button>
            <label className={styles.upload}>
              {uploading ? "Uploading…" : "Upload document"}
              <input
                type="file"
                accept=".md,.txt,.pdf,.json,.png,.jpg,.jpeg"
                hidden
                onChange={(e) => onUpload(e.target.files?.[0] || null)}
                disabled={uploading || !apiUp}
              />
            </label>
          </div>
        </section>

        <section className={styles.panel}>
          <h2>Answer</h2>
          {error ? <p className={styles.error}>{error}</p> : null}
          {statusMsg ? <p className={styles.status}>{statusMsg}</p> : null}
          {!result && !error ? (
            <p className={styles.muted}>
              Submit a question to see grounded answers with citations.
            </p>
          ) : null}
          {result ? (
            <div className={styles.answerBlock}>
              <div className={styles.metaRow}>
                <span>
                  Confidence {(result.confidence * 100).toFixed(0)}%
                </span>
                <span>
                  {result.metrics.latency_ms
                    ? `${Math.round(result.metrics.latency_ms)} ms`
                    : "—"}
                </span>
                <span>
                  {result.cache_hit
                    ? `Cache: ${result.cache_layer || "hit"}`
                    : "Fresh RAG"}
                </span>
                {result.refused ? (
                  <span className={styles.warn}>Refused</span>
                ) : null}
              </div>
              <article className={styles.answer}>{result.answer}</article>
              {result.citations?.length ? (
                <div className={styles.citations}>
                  <h3>Citations</h3>
                  <ul>
                    {result.citations.map((c) => (
                      <li key={c.chunk_id}>
                        <strong>{c.title}</strong>
                        <span className={styles.muted}>
                          {" "}
                          ({c.chunk_id}) score {c.score.toFixed(2)}
                        </span>
                        <p>{c.excerpt}</p>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {result.guardrails?.length ? (
                <p className={styles.muted}>
                  Guardrails: {result.guardrails.join(", ")}
                </p>
              ) : null}
            </div>
          ) : null}
        </section>
      </div>
    </main>
  );
}
