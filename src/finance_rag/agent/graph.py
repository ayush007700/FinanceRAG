"""LangGraph multi-node RAG agent for Source Advisors."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any, TypedDict

from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from openai import OpenAI

from finance_rag.cache import SemanticCache
from finance_rag.config import get_settings
from finance_rag.embeddings import EmbeddingService
from finance_rag.guardrails.answerability import AnswerabilityGate
from finance_rag.guardrails.pipeline import GuardrailPipeline
from finance_rag.logging_setup import get_logger
from finance_rag.metrics.retrieval_metrics import (
    citation_metrics,
    compute_online_telemetry,
)
from finance_rag.models import Citation, RAGResponse, RetrievedChunk
from finance_rag.multimodal.vision import VisionCaptioner
from finance_rag.observability import configure_langsmith
from finance_rag.retrieval import HybridRetriever

logger = get_logger(__name__)


class AgentState(TypedDict, total=False):
    query: str
    sanitized_query: str
    service_line: str | None
    image_caption: str | None
    allowed: bool
    guardrail_reasons: list[str]
    rewritten_query: str
    retrieved: list[RetrievedChunk]
    answerable: bool
    unanswerable_reason: str
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
1. Answer ONLY using the provided context passages (text and image captions). If insufficient, say so clearly.
2. This is educational / advisory decision-support — not formal legal or tax advice.
3. Cite sources inline as [chunk_id].
4. Prefer precise references to statutes, credits, and process steps.
5. Be concise, professional, and client-safe.
6. Never invent IRS/HMRC positions or dollar amounts not present in context.
7. When context includes [Image caption] passages, treat them as evidence from figures/tables.
"""


def _format_context(retrieved: list[RetrievedChunk]) -> str:
    blocks = []
    for item in retrieved:
        meta = item.chunk.metadata
        parent = meta.get("parent_excerpt")
        modality = meta.get("modality") or "text"
        header = (
            f"[{item.chunk.chunk_id}] title={meta.get('title')} "
            f"service={meta.get('service_line')} section={item.chunk.section} modality={modality}"
        )
        body = item.chunk.text
        if parent:
            body = f"(Section context) {parent}\n\n(Passage) {body}"
        blocks.append(f"{header}\n{body}")
    return "\n\n---\n\n".join(blocks)


class FinanceRAGAgent:
    def __init__(
        self,
        retriever: HybridRetriever | None = None,
        cache: SemanticCache | None = None,
    ) -> None:
        configure_langsmith()
        self.settings = get_settings()
        self.retriever = retriever or HybridRetriever()
        self.embedder = EmbeddingService()
        self.cache = cache or SemanticCache(embedder=self.embedder)
        self.guardrails = GuardrailPipeline()
        self.answerability = AnswerabilityGate()
        self.captioner = VisionCaptioner()
        # temperature=0 throughout: query rewriting is a deterministic
        # transformation, and grounded advisory answers should not vary between
        # identical requests. It also makes the eval reproducible -- at 0.1 the
        # rewritten query differed run to run, moving retrieval metrics by more
        # than the regression tolerance and making drift indistinguishable from
        # noise.
        self.llm = ChatOpenAI(
            model=self.settings.openai_chat_model,
            temperature=0,
            api_key=self.settings.openai_api_key or None,
        )
        self.openai = OpenAI(api_key=self.settings.openai_api_key or None)
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("input_guardrails", self.input_guardrails)
        graph.add_node("rewrite_query", self.rewrite_query)
        graph.add_node("retrieve", self.retrieve)
        graph.add_node("check_answerability", self.check_answerability)
        graph.add_node("generate", self.generate)
        graph.add_node("output_guardrails", self.output_guardrails)

        graph.set_entry_point("input_guardrails")
        graph.add_conditional_edges(
            "input_guardrails",
            self._route_after_input,
            {"rewrite_query": "rewrite_query", "end": END},
        )
        graph.add_edge("rewrite_query", "retrieve")
        graph.add_edge("retrieve", "check_answerability")
        # An unanswerable question skips generation entirely: no prose is
        # produced that a later stage would have to walk back, and the expensive
        # call is never made.
        graph.add_conditional_edges(
            "check_answerability",
            self._route_after_answerability,
            {"generate": "generate", "refuse": "output_guardrails"},
        )
        graph.add_edge("generate", "output_guardrails")
        graph.add_edge("output_guardrails", END)
        return graph.compile()

    def _route_after_input(self, state: AgentState) -> str:
        return "rewrite_query" if state.get("allowed", False) else "end"

    def _route_after_answerability(self, state: AgentState) -> str:
        return "generate" if state.get("answerable", True) else "refuse"

    def check_answerability(self, state: AgentState) -> AgentState:
        """Decide whether the retrieved context supports an answer at all."""
        retrieved = state.get("retrieved") or []
        verdict = self.answerability.check(state["query"], retrieved)
        if verdict.answerable:
            return {"answerable": True}

        logger.info(
            "abstained_unanswerable",
            missing=verdict.missing[:160],
            top_cosine=max((r.cosine for r in retrieved), default=0.0),
        )
        return {
            "answerable": False,
            "unanswerable_reason": verdict.missing,
            "answer": verdict.refusal_message,
            "refused": True,
            "confidence": 0.0,
            # Provenance for what was consulted and found insufficient. The
            # output guardrail narrows these to whatever the answer cites.
            "citations": self._citations_from_retrieved(retrieved),
        }

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
        if state.get("image_caption"):
            q = f"{q}\n\nUser-provided image description: {state['image_caption']}"
        prompt = (
            "Rewrite the user question for retrieval over Source Advisors tax consulting "
            "knowledge (R&D, cost segregation, 179D/45L, sales & use, ITC/PTC, property tax, LIFO). "
            "Expand acronyms and keep jurisdiction cues. Include image cues if present. "
            "Return only the rewritten query.\n\n"
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
                text=r.chunk.text,
            )
            for r in retrieved
        ]

    def generate(self, state: AgentState) -> AgentState:
        retrieved = state.get("retrieved") or []
        citations = self._citations_from_retrieved(retrieved)

        # Abstention reads raw cosine, never the fused score. RRF totals are
        # rank-derived: the top candidate scores ~1/(k+1) whether the corpus was
        # relevant or not, so thresholding them can never detect a bad retrieval.
        top_cosine = max((r.cosine for r in retrieved), default=0.0)
        avg_cosine = (
            sum(r.cosine for r in retrieved) / len(retrieved) if retrieved else 0.0
        )

        if (
            self.settings.guardrail_refusal_on_low_confidence
            and top_cosine < self.settings.min_absolute_cosine
        ):
            logger.info(
                "abstained_low_relevance",
                top_cosine=top_cosine,
                floor=self.settings.min_absolute_cosine,
                num_retrieved=len(retrieved),
            )
            return {
                "answer": (
                    "I do not have sufficiently relevant Source Advisors knowledge to answer "
                    "confidently. Please refine the question or ingest additional documentation."
                ),
                "refused": True,
                "confidence": top_cosine,
                "citations": citations,
            }

        context = _format_context(retrieved)
        extra = ""
        if state.get("image_caption"):
            extra = f"\nUser image caption:\n{state['image_caption']}\n"
        user_prompt = (
            f"Question: {state['query']}{extra}\n\nContext:\n{context}\n\n"
            "Provide a grounded answer with [chunk_id] citations."
        )
        completion = self.openai.chat.completions.create(
            model=self.settings.openai_chat_model,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        answer = completion.choices[0].message.content or ""
        return {
            "answer": answer,
            "citations": citations,
            "confidence": top_cosine if top_cosine else avg_cosine,
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

        # Report the passages the answer actually cited, not everything that was
        # retrieved. Returning all twelve candidates as "citations" overstates
        # grounding: the reader cannot tell which passage supported which claim.
        if out.grounded_ids:
            by_id = {r.chunk.chunk_id: r for r in retrieved}
            cited_chunks = [by_id[cid] for cid in out.grounded_ids if cid in by_id]
            if cited_chunks:
                citations = self._citations_from_retrieved(cited_chunks)

        latency_ms = (time.perf_counter() - state.get("started_at", time.perf_counter())) * 1000
        refused = bool(not out.allowed or already_refused)
        metrics = compute_online_telemetry(retrieved, latency_ms=latency_ms, refused=refused)

        grounding = citation_metrics(
            cited_ids=out.cited_ids,
            retrieved_ids=[r.chunk.chunk_id for r in retrieved],
        )
        metrics.citation_grounding = grounding["citation_grounding"]
        metrics.hallucinated_citations = grounding["hallucinated_citations"]

        if out.hallucinated_citations:
            logger.warning(
                "hallucinated_citations",
                ids=out.hallucinated_citations,
                grounded=len(out.grounded_ids),
                blocked=not out.allowed,
            )

        updates: AgentState = {
            "citations": citations,
            "metrics": asdict(metrics),
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

    def ask(
        self,
        query: str,
        service_line: str | None = None,
        image_bytes: bytes | None = None,
        image_mime: str = "image/png",
    ) -> RAGResponse:
        image_caption = None
        if image_bytes and self.settings.multimodal_enabled:
            image_caption = self.captioner.caption_bytes(image_bytes, mime=image_mime)
            # Cache key includes caption signal via query augmentation
            cache_query = f"{query}\n[image:{image_caption[:240]}]"
        else:
            cache_query = query

        query_embedding = None
        try:
            query_embedding = self.embedder.embed_query(
                f"{cache_query} {service_line or ''}".strip()
            )
        except Exception:  # noqa: BLE001
            query_embedding = None

        cached = self.cache.get(cache_query, service_line=service_line, query_embedding=query_embedding)
        if cached:
            return cached

        final = self.graph.invoke(
            {
                "query": query,
                "service_line": service_line,
                "image_caption": image_caption,
                "allowed": True,
                "guardrail_reasons": [],
                "citations": [],
                "retrieved": [],
            },
            config={
                "run_name": "finance_rag_ask",
                "tags": ["source-advisors", "rag"],
                "metadata": {"service_line": service_line, "has_image": bool(image_bytes)},
            },
        )
        metrics_dict = final.get("metrics") or {}
        from finance_rag.models import RetrievalMetrics

        response = RAGResponse(
            answer=final.get("answer") or "",
            citations=final.get("citations") or [],
            confidence=float(final.get("confidence") or 0.0),
            metrics=RetrievalMetrics(**metrics_dict) if metrics_dict else RetrievalMetrics(),
            guardrails=final.get("guardrail_reasons") or [],
            refused=bool(final.get("refused")),
            trace_id=final.get("trace_id"),
            cache_hit=False,
            cache_layer=None,
            retrieved_ids=[r.chunk.chunk_id for r in (final.get("retrieved") or [])],
        )
        self.cache.set(
            cache_query,
            response,
            service_line=service_line,
            query_embedding=query_embedding,
        )
        return response
