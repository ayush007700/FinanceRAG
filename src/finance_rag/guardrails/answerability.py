"""Answerability gate: does the retrieved context actually contain the answer?

Retrieval similarity cannot decide this. Measured on the golden set, questions
the corpus *can* answer score 0.591-0.799 top cosine, and questions it *cannot*
score 0.554-0.735 -- the distributions overlap almost entirely, because cosine
measures topical similarity rather than whether a specific fact is present. "How
many employees does Source Advisors have in the UK?" retrieves the UK practice
note at 0.73 similarity; that note simply never states a headcount.

So the check is semantic and explicit. It runs between retrieval and generation,
which means a refusal also skips the generation call it would otherwise pay for.

It is deliberately a separate stage rather than an instruction folded into the
generation prompt: abstention is a safety property, and it should be decided by
a step that has no incentive to produce prose and can be tested on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from openai import OpenAI

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.models import RetrievedChunk

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "You judge whether a question can be answered from provided context. "
    "You never answer the question itself. Respond with JSON only."
)

RUBRIC = """Decide whether the context contains the specific information the question asks for.

ANSWERABLE only if the context states the requested fact. Topical overlap is not
enough: context that discusses the subject area while omitting the requested
figure, count, rate, date, name or recommendation is NOT answerable.

Treat as NOT answerable:
- a request for an amount, count, percentage or rate that the context never states
- a request for information about a specific year or period the context does not cover
- a request for a recommendation about what the reader personally should do
- a question whose subject the context never addresses at all

A figure that answers a DIFFERENT question does not make this one answerable.
Before accepting a number, check it matches what was asked on every axis:
subject, basis, and period. A statutory deduction cap is not a recovery total;
a figure from one worked example is not a general maximum; a limit for one tax
year is not a limit for another. Substituting a related quantity for the one
requested is the most damaging error you can make here, because the answer will
look precise and sourced while being about something else.

Treat as ANSWERABLE when the context states the fact, even partially, provided the
partial content genuinely responds to the question.

Also ANSWERABLE when the context states the constituent facts and the question
only asks them to be compared, contrasted or listed. If the context says one
provision is a deduction for commercial buildings and another is a credit for
homes, "what is the difference" is answerable: reading two stated facts together
is not inference beyond the source. This is distinct from substituting a related
quantity, which is not permitted -- the test is whether every part of the answer
is present in the context, not whether the context phrases the conclusion.

Return exactly:
{"answerable": true|false, "missing": "<the specific information absent, or empty>",
 "supporting_ids": ["<chunk id that supports an answer>"]}"""


@dataclass
class AnswerabilityVerdict:
    answerable: bool
    missing: str = ""
    supporting_ids: list[str] = field(default_factory=list)
    checked: bool = True
    error: str | None = None

    #: Set by the caller so the wording matches what was actually searched.
    sources_consulted: str = "Source Advisors material"

    @property
    def refusal_message(self) -> str:
        base = (
            f"The available {self.sources_consulted} does not contain the information "
            "needed to answer this question."
        )
        return f"{base} Missing: {self.missing}" if self.missing else base


class AnswerabilityGate:
    def __init__(self, client: OpenAI | None = None) -> None:
        self.settings = get_settings()
        self._client = client

    def _openai(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=self.settings.openai_api_key or None)
        return self._client

    def _format_context(self, retrieved: list[RetrievedChunk]) -> str:
        limit = self.settings.answerability_passage_chars
        blocks = []
        for item in retrieved[: self.settings.answerability_max_passages]:
            blocks.append(f"[{item.chunk.chunk_id}]\n{item.chunk.text[:limit]}")
        return "\n\n---\n\n".join(blocks)

    def check(self, query: str, retrieved: list[RetrievedChunk]) -> AnswerabilityVerdict:
        if not self.settings.answerability_check_enabled:
            return AnswerabilityVerdict(answerable=True, checked=False)

        if not retrieved:
            return AnswerabilityVerdict(
                answerable=False,
                missing="no relevant material was retrieved",
            )

        prompt = (
            f"{RUBRIC}\n\nQuestion:\n{query}\n\nContext:\n{self._format_context(retrieved)}"
        )
        try:
            completion = self._openai().chat.completions.create(
                model=self.settings.openai_fast_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            payload = json.loads(completion.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001
            # Fail open. A checker outage must not turn every question into a
            # refusal -- downstream citation verification and the output
            # guardrails still apply, so the system degrades rather than stops.
            logger.warning("answerability_check_failed", error=str(exc))
            return AnswerabilityVerdict(answerable=True, checked=False, error=str(exc))

        answerable = bool(payload.get("answerable"))
        verdict = AnswerabilityVerdict(
            answerable=answerable,
            missing=str(payload.get("missing") or "").strip(),
            supporting_ids=[str(i) for i in (payload.get("supporting_ids") or [])],
        )
        logger.info(
            "answerability_checked",
            answerable=verdict.answerable,
            missing=verdict.missing[:120],
            n_passages=len(retrieved),
        )
        return verdict
