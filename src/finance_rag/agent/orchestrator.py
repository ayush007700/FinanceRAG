"""Multi-agent orchestrator.

    supervisor ─┬─ direct ──────────────────────────────┐
                ├─ researcher ─┐                        │
                └─ web_search ─┴─ answerability ─┬─ refuse ─┤
                                                 └─ analyst ─ critic ─┬─ retry ─▶ researcher
                                                                      └─ approve ─┤
                                                                                  ▼
                                                                             compliance ─▶ END

Six roles: Supervisor routes, Researcher retrieves from the corpus, WebSearch
fetches current external facts, Analyst writes the grounded answer, Critic
verifies it, Compliance applies guardrails and writes the audit record.

The Critic → Researcher edge is what makes this a graph rather than a pipeline:
a rejected draft goes back for another retrieval attempt with a reformulated
query. The loop is bounded by ``critic_max_retries`` -- an unbounded
self-correction loop is an unbounded bill.

Model budget: only the Analyst uses the full reasoning model. Routing,
answerability and criticism are classification tasks on the cheap model, and the
Researcher makes no model call at all beyond embedding, because RRF fusion runs
in SQL.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from finance_rag.agent.roles import Critic, Supervisor, WebSearchAgent
from finance_rag.config import get_settings
from finance_rag.guardrails.answerability import AnswerabilityGate
from finance_rag.guardrails.pipeline import GuardrailPipeline
from finance_rag.logging_setup import get_logger
from finance_rag.memory import AuditRecord, write_audit
from finance_rag.metrics.retrieval_metrics import citation_metrics, compute_online_telemetry
from finance_rag.models import Citation, RAGResponse, RetrievalMetrics, RetrievedChunk
from finance_rag.observability import callback_handler
from finance_rag.retrieval import HybridRetriever

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are FinanceRAG, an internal advisory assistant for Source Advisors,
a USA & UK specialised tax consulting firm (R&D Tax Credit, Cost Segregation,
Energy Efficiency §179D & §45L, Sales & Use Tax, ITC/PTC, Commercial Property
Tax, LIFO inventory).

Rules:
1. Answer ONLY from the provided passages. If they are insufficient, say so.
2. This is decision-support, not formal tax or legal advice.
3. Cite passages inline as [chunk_id]. Every factual claim needs one.
4. Never invent IRS/HMRC positions, figures, rates or dates.
5. Passages marked modality=web came from the open web just now, not from the
   firm's reviewed knowledge base. Attribute them as such and never blend them
   with firm guidance as though they carried the same authority. Where a web
   passage shows a published date, state it: a rule reported in 2023 may have
   changed, and presenting it as current is the error that matters most here.
6. Be concise and client-safe.
"""


class AgentState(TypedDict, total=False):
    query: str
    sanitized_query: str
    search_query: str
    service_line: str | None
    route: str
    rationale: str
    needs_freshness: bool
    allowed: bool
    guardrail_reasons: list[str]
    retrieved: list[RetrievedChunk]
    answerable: bool
    unanswerable_reason: str
    answer: str
    citations: list[Citation]
    confidence: float
    refused: bool
    critic_attempts: int
    critic_reasons: list[str]
    # Set by the Critic, read by its routing edge.
    _critic_approved: bool
    metrics: dict[str, Any]
    trace_id: str
    thread_id: str | None
    org_id: str | None
    as_of: Any | None
    image_caption: str | None
    started_at: float


def _search_query(query: str, image_caption: str | None) -> str:
    """Fold an attached image's caption into the retrieval query.

    An uploaded figure is often the whole question ("what does this schedule
    show?"), so its description has to reach retrieval or the search runs
    against the text alone.
    """
    if not image_caption:
        return query
    return f"{query}\n\nImage: {image_caption}"


def _format_context(retrieved: list[RetrievedChunk]) -> str:
    blocks = []
    for item in retrieved:
        meta = item.chunk.metadata
        header = (
            f"[{item.chunk.chunk_id}] title={meta.get('title')} "
            f"section={item.chunk.section} modality={meta.get('modality') or 'text'}"
        )
        if meta.get("fetched_at"):
            header += f" fetched_at={meta['fetched_at']}"
        if meta.get("published_date"):
            header += f" published={meta['published_date']}"
        body = item.chunk.text
        if meta.get("parent_excerpt"):
            body = f"(Section context) {meta['parent_excerpt']}\n\n(Passage) {body}"
        blocks.append(f"{header}\n{body}")
    return "\n\n---\n\n".join(blocks)


class MultiAgentRAG:
    def __init__(
        self,
        retriever: HybridRetriever | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        from openai import OpenAI

        self.settings = get_settings()
        self.retriever = retriever or HybridRetriever()
        self.supervisor = Supervisor()
        self.web = WebSearchAgent()
        self.critic = Critic()
        self.answerability = AnswerabilityGate()
        self.guardrails = GuardrailPipeline()
        self._captioner = None
        self._on_stage = None
        self.openai = OpenAI(api_key=self.settings.openai_api_key or None)
        self.checkpointer = checkpointer
        self.graph = self._build_graph()

    # ------------------------------------------------------------- graph

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("supervisor", self.route)
        graph.add_node("researcher", self.research)
        graph.add_node("web_search", self.search_web)
        graph.add_node("answerability", self.check_answerability)
        graph.add_node("analyst", self.analyse)
        graph.add_node("critic", self.criticise)
        graph.add_node("compliance", self.comply)

        graph.set_entry_point("supervisor")
        graph.add_conditional_edges(
            "supervisor",
            self._route_from_supervisor,
            {
                "researcher": "researcher",
                "web_search": "web_search",
                "compliance": "compliance",
            },
        )
        # "both" fans corpus retrieval into web search before converging.
        graph.add_conditional_edges(
            "researcher",
            self._after_research,
            {"web_search": "web_search", "answerability": "answerability"},
        )
        graph.add_edge("web_search", "answerability")
        graph.add_conditional_edges(
            "answerability",
            self._after_answerability,
            {"analyst": "analyst", "compliance": "compliance"},
        )
        graph.add_edge("analyst", "critic")
        # The cycle. A rejected draft returns to retrieval rather than being
        # patched in place, because the usual cause is missing evidence.
        graph.add_conditional_edges(
            "critic",
            self._after_critic,
            {"researcher": "researcher", "compliance": "compliance"},
        )
        graph.add_edge("compliance", END)
        return graph.compile(checkpointer=self.checkpointer)

    # ------------------------------------------------------------ routing

    def _route_from_supervisor(self, state: AgentState) -> str:
        if not state.get("allowed", True):
            return "compliance"
        route = state.get("route", "corpus")
        if route == "web":
            return "web_search"
        return "researcher"

    def _after_research(self, state: AgentState) -> str:
        return "web_search" if state.get("route") == "both" else "answerability"

    def _after_answerability(self, state: AgentState) -> str:
        return "analyst" if state.get("answerable", True) else "compliance"

    def _after_critic(self, state: AgentState) -> str:
        if state.get("_critic_approved", True):
            return "compliance"
        if state.get("critic_attempts", 0) > self.settings.critic_max_retries:
            return "compliance"
        return "researcher"

    # -------------------------------------------------------------- nodes

    def route(self, state: AgentState) -> AgentState:
        guard = self.guardrails.check_input(state["query"])
        updates: AgentState = {
            "allowed": guard.allowed,
            "guardrail_reasons": guard.reasons,
            "sanitized_query": guard.sanitized_text or state["query"],
            "trace_id": state.get("trace_id") or str(uuid.uuid4()),
            "started_at": time.perf_counter(),
            "critic_attempts": 0,
            "retrieved": [],
            "citations": [],
        }
        if not guard.allowed:
            updates.update(
                {
                    "answer": "I cannot process this request due to safety checks: "
                    + "; ".join(guard.reasons),
                    "refused": True,
                    "confidence": 0.0,
                    "route": "blocked",
                }
            )
            return updates

        plan = self.supervisor.plan(updates["sanitized_query"])
        updates.update(
            {
                "route": plan.route,
                "service_line": plan.service_line,
                "rationale": plan.rationale,
                "needs_freshness": plan.needs_freshness,
                "search_query": _search_query(
                    plan.search_query or updates["sanitized_query"],
                    state.get("image_caption"),
                ),
            }
        )
        return updates

    def research(self, state: AgentState) -> AgentState:
        self._emit("retrieving")
        query = state.get("search_query") or state["query"]
        # service_line is deliberately NOT passed as a retrieval filter. It is an
        # exact match against a label assigned by a first-match heuristic, and
        # that heuristic is single-valued: a document covering both §179D and
        # §45L is tagged §179D alone, so filtering a §45L question on §45L makes
        # the only document containing the answer invisible. Measured on the
        # golden set, every §45L case failed this way. RRF plus the cross-encoder
        # already handle topical focus; the label is kept for audit only.
        retrieved = self.retriever.retrieve(
            query, org_id=state.get("org_id"), as_of=state.get("as_of")
        )
        # On a retry the previous web results are preserved: the Critic rejected
        # the draft, not the sources.
        web = [r for r in (state.get("retrieved") or []) if r.chunk.metadata.get("modality") == "web"]
        return {"retrieved": retrieved + web}

    def search_web(self, state: AgentState) -> AgentState:
        self._emit("searching_web")
        query = state.get("search_query") or state["query"]
        results = self.web.search(query, freshness=bool(state.get("needs_freshness")))
        return {"retrieved": (state.get("retrieved") or []) + results}

    def check_answerability(self, state: AgentState) -> AgentState:
        self._emit("checking_answerability")
        retrieved = state.get("retrieved") or []
        verdict = self.answerability.check(state["query"], retrieved)
        if verdict.answerable:
            return {"answerable": True}
        # Name what was actually searched: telling a user the firm's material
        # lacks an answer is misleading when the web was searched too.
        modalities = {r.chunk.metadata.get("modality") for r in retrieved}
        if "web" in modalities:
            verdict.sources_consulted = (
                "Source Advisors material and current web sources"
                if len(modalities) > 1
                else "current web sources"
            )
        return {
            "answerable": False,
            "unanswerable_reason": verdict.missing,
            "answer": verdict.refusal_message,
            "refused": True,
            "confidence": 0.0,
            "citations": self._citations(retrieved),
        }

    def analyse(self, state: AgentState) -> AgentState:
        self._emit("writing")
        retrieved = state.get("retrieved") or []
        feedback = ""
        if state.get("critic_reasons"):
            feedback = (
                "\n\nA previous draft was rejected for: "
                + "; ".join(state["critic_reasons"])
                + "\nAddress those issues; do not repeat unsupported claims."
            )
        caption = state.get("image_caption")
        image_block = f"\n\nUser-provided image:\n{caption}" if caption else ""
        user_prompt = (
            f"Question: {state['query']}{image_block}"
            f"\n\nPassages:\n{_format_context(retrieved)}"
            f"{feedback}\n\nAnswer with [chunk_id] citations."
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
        top_cosine = max((r.cosine for r in retrieved), default=0.0)
        return {
            "answer": answer,
            "citations": self._citations(retrieved),
            "confidence": top_cosine,
            "refused": False,
        }

    def criticise(self, state: AgentState) -> AgentState:
        self._emit("verifying")
        retrieved = state.get("retrieved") or []
        critique = self.critic.review(state["query"], state.get("answer") or "", retrieved)
        attempts = state.get("critic_attempts", 0)
        if critique.approved:
            return {"_critic_approved": True, "critic_attempts": attempts}

        attempts += 1
        updates: AgentState = {
            "_critic_approved": False,
            "critic_attempts": attempts,
            "critic_reasons": critique.reasons or ["unsupported_claims"],
        }
        if critique.suggested_query and attempts <= self.settings.critic_max_retries:
            updates["search_query"] = critique.suggested_query
        if attempts > self.settings.critic_max_retries:
            # Budget exhausted with an unverified draft: refuse rather than ship
            # an answer the verifier would not sign off.
            updates.update(
                {
                    "answer": (
                        "I could not produce an answer fully supported by the available "
                        "Source Advisors material. Please refine the question."
                    ),
                    "refused": True,
                    "confidence": 0.0,
                }
            )
        return updates

    def comply(self, state: AgentState) -> AgentState:
        self._emit("finalising")
        retrieved = state.get("retrieved") or []
        citations = list(state.get("citations") or [])
        already_refused = bool(state.get("refused"))

        out = self.guardrails.check_output(
            query=state["query"],
            answer=state.get("answer") or "",
            citations=citations,
            retrieved=retrieved,
            already_refused=already_refused,
        )
        if out.grounded_ids:
            by_id = {r.chunk.chunk_id: r for r in retrieved}
            cited = [by_id[c] for c in out.grounded_ids if c in by_id]
            if cited:
                citations = self._citations(cited)

        latency_ms = (time.perf_counter() - state.get("started_at", time.perf_counter())) * 1000
        refused = bool(not out.allowed or already_refused)
        metrics = compute_online_telemetry(retrieved, latency_ms=latency_ms, refused=refused)
        grounding = citation_metrics(
            cited_ids=out.cited_ids, retrieved_ids=[r.chunk.chunk_id for r in retrieved]
        )
        metrics.citation_grounding = grounding["citation_grounding"]
        metrics.hallucinated_citations = grounding["hallucinated_citations"]

        answer = state.get("answer") or ""
        if not out.allowed:
            answer = out.sanitized_text or ("Response blocked: " + "; ".join(out.reasons))
        elif out.sanitized_text and not already_refused:
            answer = out.sanitized_text

        reasons = list(state.get("guardrail_reasons") or []) + out.reasons
        self._audit(state, answer, citations, out, metrics, refused, reasons, latency_ms)

        return {
            "answer": answer,
            "citations": citations,
            "refused": refused,
            "metrics": asdict(metrics),
            "guardrail_reasons": reasons,
        }

    def _emit(self, stage: str, detail: str = "") -> None:
        """Report progress to a streaming client. Never fails the request."""
        if self._on_stage is None:
            return
        try:
            self._on_stage(stage, detail)
        except Exception as exc:  # noqa: BLE001
            logger.debug("stage_emit_failed", stage=stage, error=str(exc))

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _citations(retrieved: list[RetrievedChunk]) -> list[Citation]:
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

    def _audit(self, state, answer, citations, out, metrics, refused, reasons, latency_ms) -> None:
        if not self.settings.audit_enabled:
            return
        write_audit(
            AuditRecord(
                trace_id=state.get("trace_id") or "",
                thread_id=state.get("thread_id"),
                org_id=state.get("org_id"),
                query=state["query"],
                rewritten_query=state.get("search_query"),
                service_line=state.get("service_line"),
                answer=answer,
                refused=refused,
                refusal_reason=state.get("unanswerable_reason"),
                retrieved_ids=[r.chunk.chunk_id for r in (state.get("retrieved") or [])],
                cited_ids=[c.chunk_id for c in citations],
                hallucinated_ids=out.hallucinated_citations,
                models={
                    "analyst": self.settings.openai_chat_model,
                    "router_critic": self.settings.openai_fast_model,
                    "embedding": self.settings.openai_embedding_model,
                    "reranker": self.settings.cohere_rerank_model,
                },
                route=state.get("route"),
                critic_attempts=state.get("critic_attempts", 0),
                metrics=asdict(metrics),
                guardrails=reasons,
                latency_ms=latency_ms,
            )
        )

    # --------------------------------------------------------------- entry

    @property
    def captioner(self):
        """Built lazily: most requests carry no image."""
        if self._captioner is None:
            from finance_rag.multimodal.vision import VisionCaptioner

            self._captioner = VisionCaptioner()
        return self._captioner

    def ask(
        self,
        query: str,
        thread_id: str | None = None,
        service_line: str | None = None,
        image_bytes: bytes | None = None,
        image_mime: str = "image/png",
        org_id: str | None = None,
        as_of: Any | None = None,
        on_stage: Any | None = None,
    ) -> RAGResponse:
        image_caption = None
        if image_bytes and self.settings.multimodal_enabled:
            # The caption joins the retrieval query so an attached figure can
            # steer search, and is passed to the Analyst as evidence.
            image_caption = self.captioner.caption_bytes(image_bytes, mime=image_mime)

        # Progress callback, surfaced to the SSE endpoint. Optional so the
        # non-streaming path pays nothing for it.
        self._on_stage = on_stage
        config: dict[str, Any] = {
            "run_name": "finance_rag_multi_agent",
            "metadata": {"thread_id": thread_id},
        }
        # Each graph node becomes its own span, so a slow or looping request is
        # readable by role rather than as one opaque call.
        if handler := callback_handler():
            config["callbacks"] = [handler]
        if self.checkpointer is not None:
            # A checkpointer demands a thread_id even from a caller who wants no
            # continuity, so an absent one becomes a throwaway rather than a
            # ValueError out of LangGraph. /v1/ask leaves thread_id optional and
            # the API always attaches a checkpointer, which made every
            # single-shot question a 500.
            #
            # The state below still carries the caller's thread_id unchanged: a
            # synthetic id is how this request finds its own checkpoint, not a
            # conversation anyone can resume, and the audit trail must not claim
            # otherwise.
            config["configurable"] = {
                "thread_id": thread_id or f"oneshot-{uuid.uuid4()}"
            }

        final = self.graph.invoke(
            {
                "query": query,
                "service_line": service_line,
                "thread_id": thread_id,
                "image_caption": image_caption,
                "org_id": org_id or self.settings.default_org_id,
                "as_of": as_of,
                "allowed": True,
                "guardrail_reasons": [],
                "citations": [],
                "retrieved": [],
            },
            config=config,
        )
        metrics = final.get("metrics") or {}
        return RAGResponse(
            answer=final.get("answer") or "",
            citations=final.get("citations") or [],
            confidence=float(final.get("confidence") or 0.0),
            metrics=RetrievalMetrics(**metrics) if metrics else RetrievalMetrics(),
            guardrails=final.get("guardrail_reasons") or [],
            refused=bool(final.get("refused")),
            trace_id=final.get("trace_id"),
            retrieved_ids=[r.chunk.chunk_id for r in (final.get("retrieved") or [])],
        )
