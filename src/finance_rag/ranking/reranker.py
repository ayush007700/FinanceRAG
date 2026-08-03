"""Reranking: Cohere Rerank when configured, else OpenAI cross-score heuristic."""

from __future__ import annotations

from openai import OpenAI

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.models import RetrievedChunk

logger = get_logger(__name__)


class Reranker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.openai = OpenAI(api_key=self.settings.openai_api_key or None)
        self._cohere = None
        if self.settings.use_cohere_rerank and self.settings.cohere_api_key:
            import cohere

            self._cohere = cohere.Client(self.settings.cohere_api_key)

    def rerank(
        self, query: str, candidates: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        if not candidates:
            return []
        if self._cohere:
            return self._cohere_rerank(query, candidates, top_k)
        return self._openai_relevance_rerank(query, candidates, top_k)

    def _cohere_rerank(
        self, query: str, candidates: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        docs = [c.chunk.text for c in candidates]
        response = self._cohere.rerank(
            model="rerank-english-v3.0",
            query=query,
            documents=docs,
            top_n=min(top_k, len(docs)),
        )
        reranked: list[RetrievedChunk] = []
        for item in response.results:
            candidate = candidates[item.index]
            candidate.rerank_score = float(item.relevance_score)
            candidate.score = float(item.relevance_score)
            reranked.append(candidate)
        logger.info("cohere_reranked", count=len(reranked))
        return reranked

    def _openai_relevance_rerank(
        self, query: str, candidates: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        """Lightweight LLM judge for relevance when Cohere is unavailable."""
        numbered = "\n\n".join(
            f"[{i}] {c.chunk.text[:700]}" for i, c in enumerate(candidates)
        )
        prompt = (
            "You are a retrieval reranker for Source Advisors tax consulting content. "
            "Score each passage 0-10 for usefulness answering the query. "
            "Return ONLY JSON list like "
            '[{"id":0,"score":8.5}, ...] with all ids.\n\n'
            f"Query: {query}\n\nPassages:\n{numbered}"
        )
        try:
            completion = self.openai.chat.completions.create(
                model=self.settings.openai_chat_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": 'Respond with {"scores":[{"id":0,"score":1.0}]}',
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            import json

            payload = json.loads(completion.choices[0].message.content or "{}")
            scores = payload.get("scores", payload if isinstance(payload, list) else [])
            score_map = {int(s["id"]): float(s["score"]) / 10.0 for s in scores}
            for i, candidate in enumerate(candidates):
                if i in score_map:
                    candidate.rerank_score = score_map[i]
                    # Blend original hybrid score with LLM relevance
                    candidate.score = 0.4 * candidate.score + 0.6 * score_map[i]
            logger.info("openai_reranked", count=len(candidates))
        except Exception as exc:  # noqa: BLE001
            logger.warning("rerank_fallback", error=str(exc))

        return sorted(candidates, key=lambda x: x.score, reverse=True)[:top_k]
