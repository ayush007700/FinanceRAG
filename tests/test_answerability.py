"""Answerability gate.

Abstention is a safety property in an advisory product: answering a question the
corpus cannot support is worse than declining. These tests pin the gate's
behaviour, including the failure mode it must never adopt (refusing everything
when the checker is unavailable).
"""

from __future__ import annotations

import json

import pytest

from finance_rag.config import get_settings
from finance_rag.guardrails.answerability import AnswerabilityGate, AnswerabilityVerdict
from finance_rag.models import Chunk, RetrievedChunk


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _retrieved(*pairs: tuple[str, str], cosine: float = 0.7) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk=Chunk(chunk_id=cid, doc_id="d", text=text, index=i, tokens=10),
            score=0.03,
            cosine=cosine,
        )
        for i, (cid, text) in enumerate(pairs)
    ]


class _FakeCompletion:
    def __init__(self, payload):
        self.choices = [
            type("C", (), {"message": type("M", (), {"content": json.dumps(payload)})()})()
        ]


class _FakeOpenAI:
    """Records the prompt and returns a canned verdict."""

    def __init__(self, payload=None, raises: Exception | None = None):
        self.payload = payload or {"answerable": True, "missing": "", "supporting_ids": []}
        self.raises = raises
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer.raises:
                    raise outer.raises
                return _FakeCompletion(outer.payload)

        self.chat = type("Chat", (), {"completions": _Completions()})()


UK_NOTE = ("Source Advisors operates in the USA and UK. UK R&D claims must follow "
           "HMRC guidance including additional information form requirements.")
QRE_NOTE = ("Qualified Research Expenses include wages of employees performing "
            "qualified research, supplies consumed, and contract research.")


def test_answerable_verdict_allows_generation():
    gate = AnswerabilityGate(client=_FakeOpenAI(
        {"answerable": True, "missing": "", "supporting_ids": ["c1"]}
    ))
    v = gate.check("What counts as a QRE?", _retrieved(("c1", QRE_NOTE)))
    assert v.answerable is True
    assert v.supporting_ids == ["c1"]


def test_topically_similar_but_factually_absent_is_refused():
    """The case no cosine threshold can catch.

    The UK note retrieves at 0.73 similarity and states no headcount at all.
    """
    gate = AnswerabilityGate(client=_FakeOpenAI(
        {"answerable": False, "missing": "employee headcount", "supporting_ids": []}
    ))
    v = gate.check("How many employees does Source Advisors have in the UK?",
                   _retrieved(("c1", UK_NOTE), cosine=0.73))
    assert v.answerable is False
    assert "headcount" in v.missing


def test_refusal_message_names_what_is_missing():
    v = AnswerabilityVerdict(answerable=False, missing="any stated dollar amount")
    assert "does not contain" in v.refusal_message
    assert "any stated dollar amount" in v.refusal_message


def test_refusal_message_without_a_reason_is_still_coherent():
    assert AnswerabilityVerdict(answerable=False).refusal_message.endswith("question.")


def test_empty_retrieval_refuses_without_calling_the_model():
    fake = _FakeOpenAI()
    gate = AnswerabilityGate(client=fake)
    v = gate.check("anything", [])
    assert v.answerable is False
    assert fake.calls == []  # no spend when there is nothing to judge


def test_checker_failure_fails_open():
    """A checker outage must degrade, not refuse everything.

    Citation verification and the output guardrails still apply downstream, so
    failing open loses a safety layer rather than the whole service.
    """
    gate = AnswerabilityGate(client=_FakeOpenAI(raises=RuntimeError("api down")))
    v = gate.check("What counts as a QRE?", _retrieved(("c1", QRE_NOTE)))
    assert v.answerable is True
    assert v.checked is False
    assert "api down" in (v.error or "")


def test_disabled_gate_is_a_passthrough(monkeypatch):
    monkeypatch.setenv("ANSWERABILITY_CHECK_ENABLED", "false")
    get_settings.cache_clear()
    fake = _FakeOpenAI()
    gate = AnswerabilityGate(client=fake)
    v = gate.check("q", _retrieved(("c1", QRE_NOTE)))
    assert v.answerable is True
    assert v.checked is False
    assert fake.calls == []


def test_check_uses_the_cheap_model():
    """A classification step must not burn the full reasoning model."""
    fake = _FakeOpenAI()
    AnswerabilityGate(client=fake).check("q", _retrieved(("c1", QRE_NOTE)))
    assert fake.calls[0]["model"] == get_settings().openai_fast_model
    assert fake.calls[0]["temperature"] == 0


def test_passages_are_capped(monkeypatch):
    monkeypatch.setenv("ANSWERABILITY_MAX_PASSAGES", "2")
    monkeypatch.setenv("ANSWERABILITY_PASSAGE_CHARS", "40")
    get_settings.cache_clear()
    fake = _FakeOpenAI()
    gate = AnswerabilityGate(client=fake)
    gate.check("q", _retrieved(("c1", "x" * 500), ("c2", "y" * 500), ("c3", "z" * 500)))
    prompt = fake.calls[0]["messages"][-1]["content"]
    assert "[c1]" in prompt and "[c2]" in prompt
    assert "[c3]" not in prompt
    assert "x" * 60 not in prompt  # truncated per passage


def test_malformed_model_output_is_treated_as_unanswerable():
    """Anything that is not an explicit yes must not pass the gate."""
    gate = AnswerabilityGate(client=_FakeOpenAI({"unexpected": "shape"}))
    assert gate.check("q", _retrieved(("c1", QRE_NOTE))).answerable is False


# --------------------------------------------------------------------------
# graph wiring
# --------------------------------------------------------------------------


def test_router_sends_unanswerable_questions_straight_to_the_guardrails():
    from finance_rag.agent.graph import FinanceRAGAgent

    route = FinanceRAGAgent._route_after_answerability
    assert route(None, {"answerable": True}) == "generate"
    assert route(None, {"answerable": False}) == "refuse"
    # Absent key means the gate never ran; do not refuse by accident.
    assert route(None, {}) == "generate"


def test_graph_places_the_gate_between_retrieval_and_generation():
    import inspect

    from finance_rag.agent.graph import FinanceRAGAgent

    src = inspect.getsource(FinanceRAGAgent._build_graph)
    assert 'add_edge("retrieve", "check_answerability")' in src
    assert '"generate": "generate", "refuse": "output_guardrails"' in src
