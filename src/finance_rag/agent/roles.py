"""Specialist agent roles.

Each role is a node with one job and its own model budget. Only the Analyst uses
the full reasoning model; routing and criticism are classification tasks that a
cheap model does as well, so adding agents costs less than the single expensive
reranking call this pipeline used to make on every query.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC

from openai import OpenAI

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.models import Chunk, RetrievedChunk

logger = get_logger(__name__)


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WS_RUN = re.compile(r"\s+")

# Site chrome that precedes the article on most government pages.
_CHROME_MARKERS = (
    "an official website of the united states government",
    "official websites use .gov",
    "secure .gov websites use https",
    "here's how you know",
    "skip to main content",
    "cookies on gov.uk",
)


def clean_web_text(raw: str, snippet: str = "", limit: int = 4000) -> str:
    """Strip navigation furniture from a fetched page.

    ``raw_content`` is the whole page rendered to markdown, and on a government
    site the first several thousand characters are banners, nav images and
    HTTPS notices. Truncating the head therefore captures chrome and none of the
    article -- the same failure as indexing PDF page furniture, and it made the
    answerability gate report that an IRS page about a deduction did not mention
    the deduction.

    The provider's own extract leads, because it is relevance-selected for the
    query; cleaned page text follows to give the verifier something to check
    against.
    """
    text = _MD_IMAGE.sub(" ", raw or "")
    text = _MD_LINK.sub(r"\1", text)

    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(marker in lowered for marker in _CHROME_MARKERS):
            continue
        # Nav lists and breadcrumbs: short fragments with little prose.
        if len(stripped) < 25 and not any(ch.isdigit() for ch in stripped):
            continue
        kept.append(stripped)

    body = _WS_RUN.sub(" ", " ".join(kept)).strip()
    lead = _WS_RUN.sub(" ", (snippet or "").strip())
    combined = f"{lead}\n\n{body}" if lead else body
    return combined[:limit]


# ---------------------------------------------------------------- supervisor


@dataclass
class RoutePlan:
    route: str = "corpus"  # corpus | web | both | direct
    service_line: str | None = None
    rationale: str = ""
    needs_freshness: bool = False
    search_query: str | None = None


_ROUTES = {"corpus", "web", "both"}

SUPERVISOR_PROMPT = """You route questions for a tax advisory assistant. Reply with JSON only.

Routes:
- "corpus": answerable from the firm's indexed tax knowledge. The default.
- "web":    needs current external information the indexed corpus cannot hold --
            a rate or threshold for a year the corpus predates, breaking
            legislative news, a public filing.
- "both":   needs firm knowledge and current external facts together.

Always choose one of these. There is no "no retrieval" option: retrieval is
cheap and the downstream answerability check decides whether the passages
support an answer, having actually seen them.

service_line is advisory metadata only, never a retrieval filter. When clear it
is one of: R&D Tax Credit, Cost Segregation, Energy Efficiency §179D,
Energy Efficiency §45L, Sales & Use Tax, Investment Tax Credit,
Production Tax Credit, Commercial Property Tax, LIFO Inventory; otherwise null.

search_query rewrites the question for retrieval: expand acronyms, add the
statute number where implied, keep jurisdiction cues. Return the question
unchanged if it is already a good search. Never answer it.

Return: {"route": "...", "service_line": null, "needs_freshness": false,
         "search_query": "...", "rationale": "one short clause"}"""


class Supervisor:
    """Decides what kind of question this is and who should handle it."""

    def __init__(self, client: OpenAI | None = None) -> None:
        self.settings = get_settings()
        self._client = client

    def _openai(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=self.settings.openai_api_key or None)
        return self._client

    def plan(self, query: str, history_summary: str = "") -> RoutePlan:
        prompt = query if not history_summary else f"Earlier context:\n{history_summary}\n\nQuestion:\n{query}"
        try:
            completion = self._openai().chat.completions.create(
                model=self.settings.openai_fast_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SUPERVISOR_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            payload = json.loads(completion.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001
            # Default to the corpus route: it is the safe path, being the only
            # one whose sources are curated and citable.
            logger.warning("supervisor_failed", error=str(exc))
            return RoutePlan(rationale="router unavailable; defaulted to corpus")

        route = str(payload.get("route") or "corpus").strip().lower()
        if route not in _ROUTES:
            route = "corpus"
        if route in {"web", "both"} and not self.settings.web_search_enabled:
            # Never promise a capability that is switched off.
            route = "corpus"

        plan = RoutePlan(
            route=route,
            service_line=(payload.get("service_line") or None),
            rationale=str(payload.get("rationale") or "")[:200],
            needs_freshness=bool(payload.get("needs_freshness")),
            search_query=(payload.get("search_query") or None),
        )
        logger.info("routed", route=plan.route, service_line=plan.service_line)
        return plan


# --------------------------------------------------------------- web search


class WebSearchAgent:
    """Fetches current external information as citable passages.

    Web results are converted into the same ``RetrievedChunk`` shape as corpus
    passages so they travel the identical citation, verification and audit path.
    Anything else would leave a hole in the compliance story: a claim sourced
    from the open web would reach the user unverified while corpus claims are
    checked.

    They stay distinguishable -- ``modality='web'`` plus the retrieval timestamp
    -- because "per the firm's indexed guidance" and "per a web page fetched
    today" are different assurances and the answer must not blur them.
    """

    def __init__(self, http_client=None) -> None:
        self.settings = get_settings()
        self._http = http_client

    @property
    def enabled(self) -> bool:
        return bool(self.settings.web_search_enabled and self.settings.tavily_api_key)

    def _domains(self) -> list[str]:
        raw = self.settings.web_search_include_domains or ""
        return [d.strip() for d in raw.split(",") if d.strip()]

    def search(self, query: str, freshness: bool = False) -> list[RetrievedChunk]:
        """Search the web, optionally restricted to recent news.

        ``freshness`` switches Tavily to its news topic with a recency window.
        The Supervisor sets it for questions the corpus cannot hold -- a rate for
        a year the corpus predates, a legislative change -- where a stale result
        is the specific failure to avoid.
        """
        if not self.enabled:
            logger.info("web_search_skipped", reason="disabled or no API key")
            return []

        from datetime import datetime

        import httpx

        fetched_at = datetime.now(UTC).isoformat()
        payload: dict = {
            "api_key": self.settings.tavily_api_key,
            "query": query,
            "max_results": self.settings.web_search_max_results,
            "search_depth": self.settings.web_search_depth,
            "topic": "news" if freshness else "general",
            # Snippets run a few hundred characters -- too thin for the
            # answerability gate to judge against, which reads as "the source
            # does not contain the answer" when in fact it was never shown.
            "include_raw_content": True,
        }
        if freshness:
            payload["days"] = self.settings.web_search_days
        if domains := self._domains():
            # Primary sources only. A tax figure from a content farm is a
            # liability even when it happens to be correct.
            payload["include_domains"] = domains

        try:
            client = self._http or httpx
            response = client.post(
                "https://api.tavily.com/search",
                json=payload,
                timeout=self.settings.web_search_timeout_seconds,
            )
            response.raise_for_status()
            results = (response.json() or {}).get("results") or []
        except Exception as exc:  # noqa: BLE001
            # Degrade to corpus-only rather than fail the request.
            logger.warning("web_search_failed", error=str(exc), freshness=freshness)
            return []

        chunks: list[RetrievedChunk] = []
        limit = self.settings.web_search_content_chars
        for item in results:
            content = clean_web_text(
                item.get("raw_content") or "",
                snippet=item.get("content") or "",
                limit=limit,
            )
            if not content:
                continue
            url = item.get("url") or ""
            chunk_id = f"web-{uuid.uuid5(uuid.NAMESPACE_URL, url).hex[:16]}"
            chunks.append(
                RetrievedChunk(
                    chunk=Chunk(
                        chunk_id=chunk_id,
                        doc_id=f"web-{uuid.uuid5(uuid.NAMESPACE_URL, url).hex[:8]}",
                        text=content,
                        index=0,
                        tokens=len(content) // 4,
                        section=item.get("title"),
                        metadata={
                            "title": item.get("title") or url,
                            "source": url,
                            "modality": "web",
                            "fetched_at": fetched_at,
                            # When the source itself was published, which is a
                            # different question from when we fetched it. A 2023
                            # article retrieved today still states 2023 rules.
                            "published_date": item.get("published_date"),
                            "topic": "news" if freshness else "general",
                        },
                    ),
                    # Provider relevance is not comparable with corpus cosine, so
                    # it is recorded as a rerank score and never as `cosine` --
                    # the abstention gate reads cosine and must not be fed a
                    # number from a different scale.
                    score=float(item.get("score") or 0.0),
                    rerank_score=float(item.get("score") or 0.0),
                )
            )
        logger.info("web_search_completed", results=len(chunks))
        return chunks


# ------------------------------------------------------------------- critic


@dataclass
class Critique:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    suggested_query: str | None = None
    checked: bool = True


CRITIC_PROMPT = """You verify a drafted answer against the passages it was built from.
You do not rewrite the answer. Reply with JSON only.

Reject the draft when:
- it states a fact the passages do not support
- it gives a figure, rate or date the passages do not contain
- it cites a passage id that is not in the provided list
- it answers a different question than the one asked

Approve when every claim traces to a passage, even if the answer is brief or
declines to answer. A refusal is always approvable: declining invents nothing.

If rejecting and a better search would help, propose one reformulated query.

Return: {"approved": true, "reasons": [], "unsupported_claims": [],
         "suggested_query": null}"""


class Critic:
    """Verifies the draft before it reaches the user.

    Separate from the answerability gate on purpose: that gate asks whether the
    context *could* support an answer, this asks whether the answer actually
    produced *is* supported. Improving retrieval makes the first question easier
    and the second harder, because better passages make more convincing
    distractors -- so both checks are needed.
    """

    def __init__(self, client: OpenAI | None = None) -> None:
        self.settings = get_settings()
        self._client = client

    def _openai(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=self.settings.openai_api_key or None)
        return self._client

    def review(
        self, query: str, answer: str, retrieved: list[RetrievedChunk]
    ) -> Critique:
        if not self.settings.critic_enabled:
            return Critique(approved=True, checked=False)
        if not answer.strip():
            return Critique(approved=False, reasons=["empty_answer"])

        passages = "\n\n---\n\n".join(
            f"[{r.chunk.chunk_id}]\n{r.chunk.text[:1500]}" for r in retrieved[:8]
        )
        prompt = (
            f"Question:\n{query}\n\nPassages:\n{passages}\n\nDraft answer:\n{answer}"
        )
        try:
            completion = self._openai().chat.completions.create(
                model=self.settings.openai_fast_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": CRITIC_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            payload = json.loads(completion.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001
            # Fail open: the deterministic citation checks in the output
            # guardrail still run, so a critic outage loses a layer rather than
            # the service.
            logger.warning("critic_failed", error=str(exc))
            return Critique(approved=True, checked=False)

        critique = Critique(
            approved=bool(payload.get("approved")),
            reasons=[str(r) for r in (payload.get("reasons") or [])][:5],
            unsupported_claims=[str(c) for c in (payload.get("unsupported_claims") or [])][:5],
            suggested_query=(payload.get("suggested_query") or None),
        )
        logger.info(
            "critique",
            approved=critique.approved,
            reasons=critique.reasons,
            has_suggestion=bool(critique.suggested_query),
        )
        return critique
