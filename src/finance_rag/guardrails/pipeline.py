"""Input/output guardrails for finance RAG."""

from __future__ import annotations

import re

from finance_rag.config import get_settings
from finance_rag.models import Citation, GuardrailResult, RetrievedChunk

DISALLOWED_PATTERNS = [
    r"\bignore (all|previous) instructions\b",
    r"\bjailbreak\b",
    r"\bexfiltrate\b",
    r"\binsert .* system prompt\b",
]

PII_PATTERNS = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]"),
    (r"\b(?:\d[ -]*?){13,19}\b", "[REDACTED_CARD]"),
    (r"\b[A-Z]{2}\d{6}[A-Z]\b", "[REDACTED_NINO]"),  # rough UK NINO-ish
]

INJECTION_MARKERS = ["</system>", "<|im_start|>", "SYSTEM:"]

# Chunk ids are "{doc_id}_{16 hex}". doc_id is either a path hash (hex) or a
# corpus-supplied key such as "itc-ptc-001", so hyphens must be accepted --
# without them, citations to the JSON-sourced service-line documents parse as
# nothing and a correctly cited answer is reported as having no citations.
CITATION_RE = re.compile(r"\[([A-Za-z0-9_-]{4,})\]")

# Bracketed text that is clearly not a citation (footnote markers, our own PII
# placeholders) and must not be counted as a fabricated id.
_NON_CITATION = re.compile(r"^(?:\d+|REDACTED_[A-Z]+)$")


class GuardrailPipeline:
    def __init__(self) -> None:
        self.settings = get_settings()

    def check_input(self, text: str) -> GuardrailResult:
        reasons: list[str] = []
        sanitized = text.strip()

        if len(sanitized) > self.settings.guardrail_max_input_chars:
            reasons.append("input_too_long")
            return GuardrailResult(allowed=False, reasons=reasons, risk_score=0.9)

        if not sanitized:
            return GuardrailResult(allowed=False, reasons=["empty_input"], risk_score=1.0)

        lowered = sanitized.lower()
        for pattern in DISALLOWED_PATTERNS:
            if re.search(pattern, lowered):
                reasons.append("prompt_injection")
        for marker in INJECTION_MARKERS:
            if marker.lower() in lowered:
                reasons.append("control_token_injection")

        if self.settings.guardrail_block_pii:
            for pattern, repl in PII_PATTERNS:
                if re.search(pattern, sanitized):
                    reasons.append("pii_detected")
                    sanitized = re.sub(pattern, repl, sanitized)

        # Soft-block only hard injection; PII is redacted and allowed
        hard_block = any(r in {"prompt_injection", "control_token_injection"} for r in reasons)
        return GuardrailResult(
            allowed=not hard_block,
            reasons=sorted(set(reasons)),
            sanitized_text=sanitized,
            risk_score=0.8 if hard_block else (0.3 if reasons else 0.0),
        )

    def check_output(
        self,
        query: str,
        answer: str,
        citations: list[Citation],
        retrieved: list[RetrievedChunk],
        *,
        already_refused: bool = False,
    ) -> GuardrailResult:
        reasons: list[str] = []
        sanitized = answer

        # Disclaimer for tax content (informational — never a hard block)
        if (
            not already_refused
            and "not formal" not in answer.lower()
            and "not legal" not in answer.lower()
        ):
            sanitized = (
                answer.rstrip()
                + "\n\n_Note: This is decision-support content for Source Advisors teams "
                "and partners, not formal tax or legal advice._"
            )
            reasons.append("added_disclaimer")

        # Soft flag only: callers should rebuild citations from retrieved when empty
        if self.settings.guardrail_require_citations and retrieved and not citations:
            reasons.append("citations_rebuilt_from_retrieval")

        # Citation verification. The previous implementation built the set of
        # known ids and then only tested it for emptiness, so an id the model
        # invented was never detected -- the single most important check in an
        # advisory product, since a fabricated citation is an answer that cannot
        # be audited back to a source.
        known = {r.chunk.chunk_id for r in retrieved} | {c.chunk_id for c in citations}
        cited_ids: list[str] = []
        for token in CITATION_RE.findall(answer):
            if _NON_CITATION.match(token) or token in cited_ids:
                continue
            cited_ids.append(token)

        grounded = [cid for cid in cited_ids if cid in known]
        hallucinated = [cid for cid in cited_ids if cid not in known]

        if self.settings.guardrail_require_citations and retrieved and not already_refused:
            if not cited_ids and known:
                reasons.append("no_inline_citations")
            if hallucinated:
                reasons.append("hallucinated_citation")
                # Every citation fabricated means the answer claims grounding it
                # does not have. A partially fabricated answer is flagged and
                # kept, so a downstream verifier can weigh it against the
                # citations that do resolve.
                if not grounded:
                    return GuardrailResult(
                        allowed=False,
                        reasons=sorted(set(reasons)),
                        risk_score=0.9,
                        cited_ids=cited_ids,
                        grounded_ids=grounded,
                        hallucinated_citations=hallucinated,
                    )

        # Block absolute unauthorized advice language
        if re.search(r"\byou (must|should) file\b.*\btoday\b", answer, flags=re.IGNORECASE):
            reasons.append("overconfident_directive")
            return GuardrailResult(
                allowed=False,
                reasons=reasons,
                risk_score=0.6,
            )

        for pattern, repl in PII_PATTERNS:
            sanitized = re.sub(pattern, repl, sanitized)

        return GuardrailResult(
            allowed=True,
            reasons=sorted(set(reasons)),
            sanitized_text=sanitized,
            risk_score=0.5 if hallucinated else (0.1 if reasons else 0.0),
            cited_ids=cited_ids,
            grounded_ids=grounded,
            hallucinated_citations=hallucinated,
        )
