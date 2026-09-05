"""LLM-as-judge faithfulness and answer relevance.

Judged against the *full* passages the generator saw. Grading faithfulness
against 280-character citation excerpts systematically under-reports it: the
judge marks a claim unsupported because the evidence was truncated away, not
because the model invented it.
"""

from __future__ import annotations

import json

from openai import OpenAI

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.models import RetrievalMetrics

logger = get_logger(__name__)

_JUDGE_SYSTEM = (
    "You grade retrieval-augmented answers for a tax advisory assistant. "
    "You are strict: a claim counts as supported only if the context states it. "
    "Respond with JSON only."
)

_RUBRIC = """Score the answer on two axes, each 0.0-1.0.

faithfulness: the share of factual claims in the answer that the context
supports. A refusal or an explicit "insufficient information" answer is fully
faithful (1.0) -- declining to answer invents nothing. Statutory citations,
figures and dates not present in the context are unfaithful.

answer_relevance: how directly the answer addresses the question. A correct but
evasive answer scores low. A refusal to a question the context cannot support
is relevant (1.0); a refusal to a question the context *does* cover is not.

Return exactly:
{"faithfulness": 0.0, "answer_relevance": 0.0, "unsupported_claims": [], "notes": ""}
"""


def judge_answer(
    query: str,
    answer: str,
    contexts: list[str],
    max_context_chars: int = 4000,
) -> RetrievalMetrics:
    """Grade one answer. ``contexts`` should be full passage text.

    Passages are truncated only at ``max_context_chars`` each, which exists to
    bound judge cost -- not at citation-excerpt length.
    """
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key or None)

    if not contexts:
        # No context means nothing could have been grounded; scoring it as a
        # faithfulness failure would blame the generator for a retrieval miss.
        return RetrievalMetrics(faithfulness=None, answer_relevance=None)

    blocks = [f"[context {i}]\n{c[:max_context_chars]}" for i, c in enumerate(contexts, 1)]
    joined = "\n\n---\n\n".join(blocks)

    prompt = (
        f"{_RUBRIC}\n\nQuestion:\n{query}\n\n"
        f"Context:\n{joined}\n\nAnswer:\n{answer}\n"
    )
    try:
        completion = client.chat.completions.create(
            model=settings.openai_chat_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        payload = json.loads(completion.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        # A judge outage must read as "unmeasured", never as a score of zero --
        # a zero would look like a quality regression in the eval report.
        logger.warning("judge_failed", error=str(exc))
        return RetrievalMetrics(faithfulness=None, answer_relevance=None)

    def _score(key: str) -> float | None:
        value = payload.get(key)
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return None

    return RetrievalMetrics(
        faithfulness=_score("faithfulness"),
        answer_relevance=_score("answer_relevance"),
    )
