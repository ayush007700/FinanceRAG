"""Lightweight LLM-as-judge faithfulness / answer relevance."""

from __future__ import annotations

import json

from openai import OpenAI

from finance_rag.config import get_settings
from finance_rag.models import RetrievalMetrics


def judge_answer(
    query: str, answer: str, contexts: list[str]
) -> RetrievalMetrics:
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key or None)
    joined = "\n---\n".join(contexts[:6])
    prompt = f"""Score the RAG answer from 0 to 1.
Return JSON: {{"faithfulness":0.0,"answer_relevance":0.0,"notes":"..."}}

Faithfulness: claims supported by context.
Answer relevance: answers the user question.

Question: {query}

Context:
{joined}

Answer:
{answer}
"""
    completion = client.chat.completions.create(
        model=settings.openai_chat_model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    payload = json.loads(completion.choices[0].message.content or "{}")
    return RetrievalMetrics(
        faithfulness=float(payload.get("faithfulness", 0)),
        answer_relevance=float(payload.get("answer_relevance", 0)),
    )
