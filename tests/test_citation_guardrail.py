"""Citation verification in the output guardrail.

A fabricated citation is the failure mode that matters most in an advisory
product: the answer looks auditable and is not. The previous implementation
built the set of known chunk ids and then only tested it for emptiness, so no
invented id was ever detected.
"""

from __future__ import annotations

from finance_rag.guardrails import GuardrailPipeline
from finance_rag.models import Chunk, RetrievedChunk


def _retrieved(*chunk_ids: str) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk=Chunk(chunk_id=cid, doc_id="d", text="body text", index=0, tokens=2),
            score=0.03,
            cosine=0.7,
        )
        for cid in chunk_ids
    ]


HEX_A = "8f2f61410b1599ca_cc6932e92a7c2efe"
HEX_B = "5c7c314740cc3c30_aa11bb22cc33dd44"
HYPHENATED = "lifo-001_57f4d6d26ad7504a"


def _check(answer: str, chunk_ids: tuple[str, ...] = (HEX_A, HEX_B)):
    return GuardrailPipeline().check_output(
        query="q", answer=answer, citations=[], retrieved=list(_retrieved(*chunk_ids))
    )


def test_hyphenated_chunk_ids_are_parsed():
    """Regression: doc_ids like 'lifo-001' contain hyphens.

    The old pattern was [a-zA-Z0-9_]+, so citations to every JSON-sourced
    service-line document failed to parse and a correctly cited answer was
    reported as having no citations at all.
    """
    r = _check(f"LIFO defers income [{HYPHENATED}].", (HYPHENATED,))
    assert r.cited_ids == [HYPHENATED]
    assert r.grounded_ids == [HYPHENATED]
    assert r.hallucinated_citations == []
    assert "no_inline_citations" not in r.reasons


def test_valid_citation_passes_clean():
    r = _check(f"The four-part test applies [{HEX_A}].")
    assert r.allowed is True
    assert r.hallucinated_citations == []
    assert "hallucinated_citation" not in r.reasons


def test_fabricated_id_is_detected():
    r = _check(f"See [{HEX_A}] and [chunk_totally_made_up].")
    assert "hallucinated_citation" in r.reasons
    assert r.hallucinated_citations == ["chunk_totally_made_up"]


def test_partial_fabrication_is_flagged_but_allowed():
    """One bad id among good ones is reported, not blocked.

    A downstream verifier can weigh it against the citations that do resolve.
    """
    r = _check(f"See [{HEX_A}] and [chunk_totally_made_up].")
    assert r.allowed is True
    assert r.grounded_ids == [HEX_A]
    assert r.risk_score >= 0.5


def test_wholly_fabricated_citations_are_blocked():
    """Every citation invented means the answer claims grounding it lacks."""
    r = _check("Per [fake_aaaaaaaaaaaaaaaa] and [fake_bbbbbbbbbbbbbbbb].")
    assert r.allowed is False
    assert "hallucinated_citation" in r.reasons
    assert len(r.hallucinated_citations) == 2


def test_footnote_markers_are_not_treated_as_citations():
    r = _check("As noted [1] and [2], the rules differ.")
    assert r.hallucinated_citations == []
    assert "no_inline_citations" in r.reasons


def test_pii_placeholders_are_not_treated_as_citations():
    """Our own redaction markers must not read as fabricated ids."""
    r = _check(f"The taxpayer [REDACTED_SSN] qualifies [{HEX_A}].")
    assert r.hallucinated_citations == []
    assert "hallucinated_citation" not in r.reasons


def test_repeated_citation_counted_once():
    r = _check(f"[{HEX_A}] confirms this, see also [{HEX_A}].")
    assert r.cited_ids == [HEX_A]


def test_missing_citations_flagged_when_context_was_retrieved():
    r = _check("The four-part test applies to qualified research.")
    assert "no_inline_citations" in r.reasons
    assert r.allowed is True


def test_refused_answers_are_not_required_to_cite():
    r = GuardrailPipeline().check_output(
        query="q",
        answer="I do not have sufficiently relevant knowledge to answer.",
        citations=[],
        retrieved=_retrieved(HEX_A),
        already_refused=True,
    )
    assert r.allowed is True
    assert "no_inline_citations" not in r.reasons
    assert "hallucinated_citation" not in r.reasons
