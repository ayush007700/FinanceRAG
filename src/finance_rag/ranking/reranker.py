"""Reranking stage.

RRF gives a good candidate ordering from two cheap rankers, but it only knows
rank agreement -- it never reads the passage against the query. A cross-encoder
does, which is why reranking is usually the largest single quality gain in the
retrieval stack.

Providers (``RERANK_PROVIDER``):

``cohere``
    Hosted cross-encoder. Purpose-built, sub-second, priced per search rather
    than per token.
``llm``
    A chat model scoring passages in one call. Roughly 3x the cost of the hosted
    reranker and seconds of added latency because it sits on the critical path.
    Kept so the two can be compared on the same eval set, not as a default.
``none``
    Trust RRF order. Free, and the honest baseline to measure the others against.

Failure policy: if the configured provider errors or times out, fall back to the
incoming RRF order -- never to the LLM path. A degraded ranking is recoverable;
silently swapping to a slower, pricier provider during an outage is not.
"""

from __future__ import annotations

import json

from openai import OpenAI

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.models import RetrievedChunk

logger = get_logger(__name__)


class Reranker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.provider = self.settings.rerank_provider
        self._cohere = None
        self._openai: OpenAI | None = None

        if self.provider == "cohere":
            if self.settings.cohere_api_key:
                import cohere

                self._cohere = cohere.Client(
                    api_key=self.settings.cohere_api_key,
                    timeout=self.settings.rerank_timeout_seconds,
                    max_retries=self.settings.rerank_max_retries,
                )
            else:
                # Degrade loudly: an unset key would otherwise look like a
                # working reranker that quietly never reranks.
                logger.warning(
                    "cohere_rerank_unavailable",
                    reason="COHERE_API_KEY not set",
                    falling_back_to="rrf_order",
                )

    @property
    def active_provider(self) -> str:
        """Provider actually in effect, after credential checks."""
        if self.provider == "cohere" and self._cohere is None:
            return "none"
        return self.provider

    def _openai_client(self) -> OpenAI:
        if self._openai is None:
            self._openai = OpenAI(api_key=self.settings.openai_api_key or None)
        return self._openai

    def rerank(
        self, query: str, candidates: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        if not candidates:
            return []

        provider = self.active_provider
        if provider == "cohere":
            try:
                return self._cohere_rerank(query, candidates, top_k)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "rerank_failed", provider="cohere", error=str(exc),
                    falling_back_to="rrf_order",
                )
                return self._rrf_order(candidates, top_k)

        if provider == "llm":
            try:
                return self._llm_rerank(query, candidates, top_k)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "rerank_failed", provider="llm", error=str(exc),
                    falling_back_to="rrf_order",
                )
                return self._rrf_order(candidates, top_k)

        return self._rrf_order(candidates, top_k)

    @staticmethod
    def _rrf_order(candidates: list[RetrievedChunk], top_k: int) -> list[RetrievedChunk]:
        """Baseline: keep fusion order, truncate. Leaves scores untouched."""
        return sorted(candidates, key=lambda x: x.score, reverse=True)[:top_k]

    def _cohere_rerank(
        self, query: str, candidates: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        assert self._cohere is not None
        documents = [c.chunk.text for c in candidates]
        response = self._cohere.rerank(
            model=self.settings.cohere_rerank_model,
            query=query,
            documents=documents,
            top_n=min(top_k, len(documents)),
        )

        reranked: list[RetrievedChunk] = []
        for item in response.results:
            candidate = candidates[item.index]
            candidate.rerank_score = float(item.relevance_score)
            # Replace rather than blend. The incoming score is an RRF total
            # (~0.03, ordinal); relevance_score is absolute in [0, 1]. Averaging
            # the two mixes incompatible scales. rrf_score keeps the fusion value
            # for diagnostics, and cosine stays untouched for the abstention gate.
            candidate.score = float(item.relevance_score)
            reranked.append(candidate)

        logger.info(
            "reranked",
            provider="cohere",
            model=self.settings.cohere_rerank_model,
            candidates=len(documents),
            returned=len(reranked),
        )
        return reranked

    def _llm_rerank(
        self, query: str, candidates: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        """Chat model scoring every passage. Comparison baseline only."""
        numbered = "\n\n".join(f"[{i}] {c.chunk.text[:700]}" for i, c in enumerate(candidates))
        prompt = (
            "You are a retrieval reranker for Source Advisors tax consulting content. "
            "Score each passage 0-10 for usefulness answering the query. "
            'Return ONLY JSON like {"scores":[{"id":0,"score":8.5}]} with all ids.\n\n'
            f"Query: {query}\n\nPassages:\n{numbered}"
        )
        completion = self._openai_client().chat.completions.create(
            model=self.settings.openai_chat_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": 'Respond with {"scores":[{"id":0,"score":1.0}]}'},
                {"role": "user", "content": prompt},
            ],
        )
        payload = json.loads(completion.choices[0].message.content or "{}")
        scores = payload.get("scores", payload if isinstance(payload, list) else [])
        score_map = {int(s["id"]): float(s["score"]) / 10.0 for s in scores}

        for i, candidate in enumerate(candidates):
            if i in score_map:
                candidate.rerank_score = score_map[i]
                candidate.score = score_map[i]

        logger.info("reranked", provider="llm", candidates=len(candidates))
        return sorted(candidates, key=lambda x: x.score, reverse=True)[:top_k]
