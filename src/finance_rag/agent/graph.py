"""LangGraph multi-node RAG agent for Source Advisors."""

from __future__ import annotations

import time
import uuid
from typing import Any, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from openai import OpenAI

from finance_rag.config import get_settings
from finance_rag.guardrails.pipeline import GuardrailPipeline
from finance_rag.logging_setup import get_logger
from finance_rag.metrics.retrieval_metrics import compute_online_metrics
from finance_rag.models import Citation, RAGResponse, RetrievedChunk
from finance_rag.retrieval import HybridRetriever

logger = get_logger(__name__)


class AgentState(TypedDict, total=False):
    query: str
    sanitized_query: str
    service_line: str | None
    allowed: bool
    guardrail_reasons: list[str]
    rewritten_query: str
    retrieved: list[RetrievedChunk]
    answer: str
    citations: list[Citation]
    confidence: float
    refused: bool
    metrics: dict[str, Any]
    trace_id: str
    started_at: float


SYSTEM_PROMPT = """You are FinanceRAG, an internal advisory assistant for Source Advisors.

Source Advisors is a global (USA & UK) specialized tax consulting firm providing:
R&D Tax Credit, Cost Segregation, Energy Efficiency (§179D & §45L), Sales & Use Tax,
Investment & Production Tax Credits, Commercial Property Tax Consulting, and LIFO inventory solutions.
Core values: trust, integrity, and hard work. Partnerships with prominent accounting firms,
associations, and Fortune 1000 companies.

Rules:
1. Answer ONLY using the provided context passages. If insufficient, say so clearly.
2. This is educational / advisory decision-support — not formal legal or tax advice.
3. Cite sources inline as [chunk_id].
4. Prefer precise references to statutes, credits, and process steps.
5. Be concise, professional, and client-safe.
6. Never invent IRS/HMRC positions or dollar amounts not present in context.
"""


def _format_context(retrieved: list[RetrievedChunk]) -> str:
    blocks = []
    for item in retrieved:
        meta = item.chunk.metadata
        parent = meta.get("parent_excerpt")
        header = (
            f"[{item.chunk.chunk_id}] title={meta.get('title')} "
            f"service={meta.get('service_line')} section={item.chunk.section}"
        )
        body = item.chunk.text
        if parent:
            body = f"(Section context) {parent}\n\n(Passage) {body}"
        blocks.append(f"{header}\n{body}")
    return "\n\n---\n\n".join(blocks)


class FinanceRAGAgent:
    def __init__(self, retriever: HybridRetriever | None = None) -> None:
        self.settings = get_settings()
        self.retriever = retriever or HybridRetriever()
        self.guardrails = GuardrailPipeline()
        self.llm = ChatOpenAI(
            model=self.settings.openai_chat_model,
            temperature=0.1,
            api_key=self.settings.openai_api_key or None,
        )
        self.openai = OpenAI(api_key=self.settings.openai_api_key or None)
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("input_guardrails", self.input_guardrails)
        graph.add_node("rewrite_query", self.rewrite_query)
        graph.add_node("retrieve", self.retrieve)
        graph.add_node("generate", self.generate)
        graph.add_node("output_guardrails", self.output_guardrails)

        graph.set_entry_point("input_guardrails")
        graph.add_conditional_edges(
            "input_guardrails",
            self._route_after_input,
            {"rewrite_query": "rewrite_query", "end": END},
        )
        graph.add_edge("rewrite_query", "retrieve")
        graph.add_edge("retrieve", "generate")
        graph.add_edge("generate", "output_guardrails")
        graph.add_edge("output_guardrails", END)
        return graph.compile()

    def _route_after_input(self, state: AgentState) -> str:
        return "rewrite_query" if state.get("allowed", False) else "end"

    def input_guardrails(self, state: AgentState) -> AgentState:
        result = self.guardrails.check_input(state["query"])
        updates: AgentState = {
            "allowed": result.allowed,
            "guardrail_reasons": result.reasons,
            "sanitized_query": result.sanitized_text or state["query"],
            "trace_id": state.get("trace_id") or str(uuid.uuid4()),
            "started_at": time.perf_counter(),
        }
        if not result.allowed:
            updates.update(
                {
                    "answer": (
                        "I cannot process this request due to safety / policy checks: "
                        + "; ".join(result.reasons)
                    ),
                    "refused": True,
                    "confidence": 0.0,
                    "citations": [],
                    "metrics": {},
                }
            )
        return updates

    def rewrite_query(self, state: AgentState) -> AgentState:
        q = state.get("sanitized_query") or state["query"]
        prompt = (
            "Rewrite the user question for retrieval over Source Advisors tax consulting "
            "knowledge (R&D, cost segregation, 179D/45L, sales & use, ITC/PTC, property tax, LIFO). "
            "Expand acronyms and keep jurisdiction cues. Return only the rewritten query.\n\n"
            f"Question: {q}"
        )
        try:
            msg = self.llm.invoke(prompt)
            rewritten = msg.content.strip()
        except Exception:  # noqa: BLE001
            rewritten = q
        return {"rewritten_query": rewritten}

    def retrieve(self, state: AgentState) -> AgentState:
        query = state.get("rewritten_query") or state.get("sanitized_query") or state["query"]
        retrieved = self.retriever.retrieve(query, service_line=state.get("service_line"))
        return {"retrieved": retrieved}

    @staticmethod
    def _citations_from_retrieved(retrieved: list[RetrievedChunk]) -> list[Citation]:
        return [
            Citation(
                chunk_id=r.chunk.chunk_id,
                doc_id=r.chunk.doc_id,
                title=str(r.chunk.metadata.get("title") or "Untitled"),
                source=str(r.chunk.metadata.get("source") or ""),
                excerpt=r.chunk.text[:280],
                score=r.score,
            )
            for r in retrieved
        ]

    def generate(self, state: AgentState) -> AgentState:
        retrieved = state.get("retrieved") or []
        # Use best hit for the gate — average of top-K is often depressed after fusion.
        top_rel = max((r.score for r in retrieved), default=0.0)
        avg_rel = sum(r.score for r in retrieved) / len(retrieved) if retrieved else 0.0
        citations = self._citations_from_retrieved(retrieved)

        if (
            self.settings.guardrail_refusal_on_low_confidence
            and top_rel < self.settings.guardrail_min_relevance
        ):
            return {
                "answer": (
                    "I do not have sufficiently relevant Source Advisors knowledge to answer "
                    "confidently. Please refine the question or ingest additional documentation."
                ),
                "refused": True,
                "confidence": top_rel,
                "citations": citations,
            }

        context = _format_context(retrieved)
        user_prompt = (
            f"Question: {state['query']}\n\nContext:\n{context}\n\n"
            "Provide a grounded answer with [chunk_id] citations."
        )
        completion = self.openai.chat.completions.create(
            model=self.settings.openai_chat_model,
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        answer = completion.choices[0].message.content or ""
        return {
            "answer": answer,
            "citations": citations,
            "confidence": top_rel if top_rel else avg_rel,
            "refused": False,
        }

    def output_guardrails(self, state: AgentState) -> AgentState:
        answer = state.get("answer") or ""
        retrieved = state.get("retrieved") or []
        citations = list(state.get("citations") or [])
        if retrieved and not citations:
            citations = self._citations_from_retrieved(retrieved)

        already_refused = bool(state.get("refused"))
        out = self.guardrails.check_output(
            query=state["query"],
            answer=answer,
            citations=citations,
            retrieved=retrieved,
            already_refused=already_refused,
        )
        latency_ms = (time.perf_counter() - state.get("started_at", time.perf_counter())) * 1000
        metrics = compute_online_metrics(retrieved, latency_ms=latency_ms)

        updates: AgentState = {
            "citations": citations,
            "metrics": {
                "hit_rate": metrics.hit_rate,
                "mrr": metrics.mrr,
                "ndcg": metrics.ndcg,
                "latency_ms": metrics.latency_ms,
                "num_retrieved": metrics.num_retrieved,
                "avg_relevance": metrics.avg_relevance,
            },
            "guardrail_reasons": list(state.get("guardrail_reasons") or []) + out.reasons,
        }
        if not out.allowed:
            updates["answer"] = out.sanitized_text or (
                "Response blocked by output guardrails: " + "; ".join(out.reasons)
            )
            updates["refused"] = True
        elif out.sanitized_text and not already_refused:
            updates["answer"] = out.sanitized_text
        return updates

    def ask(self, query: str, service_line: str | None = None) -> RAGResponse:
        final = self.graph.invoke(
            {
                "query": query,
                "service_line": service_line,
                "allowed": True,
                "guardrail_reasons": [],
                "citations": [],
                "retrieved": [],
            }
        )
        metrics_dict = final.get("metrics") or {}
        from finance_rag.models import RetrievalMetrics

        return RAGResponse(
            answer=final.get("answer") or "",
            citations=final.get("citations") or [],
            confidence=float(final.get("confidence") or 0.0),
            metrics=RetrievalMetrics(**metrics_dict) if metrics_dict else RetrievalMetrics(),
            guardrails=final.get("guardrail_reasons") or [],
            refused=bool(final.get("refused")),
            trace_id=final.get("trace_id"),
        )
