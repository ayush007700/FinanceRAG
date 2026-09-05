"""Reranker provider selection, score semantics and failure policy."""

from __future__ import annotations

import pytest

from finance_rag.config import get_settings
from finance_rag.models import Chunk, RetrievedChunk


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Settings are lru_cached; drop the cache around env-var overrides."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _candidates() -> list[RetrievedChunk]:
    """Three candidates in RRF order, each with a distinct absolute cosine."""
    specs = [
        ("c1", "four-part test qualified research", 0.032, 0.81),
        ("c2", "cost segregation accelerates depreciation", 0.031, 0.55),
        ("c3", "LIFO inventory IPIC computation", 0.016, 0.22),
    ]
    return [
        RetrievedChunk(
            chunk=Chunk(chunk_id=cid, doc_id="d1", text=txt, index=i, tokens=5),
            score=rrf,
            rrf_score=rrf,
            cosine=cos,
        )
        for i, (cid, txt, rrf, cos) in enumerate(specs)
    ]


class _FakeResult:
    def __init__(self, index: int, relevance_score: float):
        self.index = index
        self.relevance_score = relevance_score


class _FakeResponse:
    def __init__(self, results):
        self.results = results


class _FakeCohere:
    """Reverses the incoming order, so a no-op would be visible as a failure."""

    def __init__(self):
        self.calls = []

    def rerank(self, *, model, query, documents, top_n):
        self.calls.append({"model": model, "query": query, "n_docs": len(documents),
                           "top_n": top_n})
        scored = [_FakeResult(i, 0.1 * (i + 1)) for i in range(len(documents))]
        scored.sort(key=lambda r: r.relevance_score, reverse=True)
        return _FakeResponse(scored[:top_n])


class _ExplodingCohere:
    def rerank(self, **kwargs):
        raise RuntimeError("cohere is down")


def _reranker(monkeypatch, provider="cohere", client=None, api_key="test-key"):
    monkeypatch.setenv("RERANK_PROVIDER", provider)
    monkeypatch.setenv("COHERE_API_KEY", api_key)
    get_settings.cache_clear()
    from finance_rag.ranking.reranker import Reranker

    r = Reranker.__new__(Reranker)
    r.settings = get_settings()
    r.provider = provider
    r._cohere = client
    r._openai = None
    return r


def test_cohere_is_the_default_provider():
    assert get_settings().rerank_provider == "cohere"


def test_default_model_is_v35():
    assert get_settings().cohere_rerank_model == "rerank-v3.5"


def test_cohere_rerank_reorders_and_sets_absolute_scores(monkeypatch):
    fake = _FakeCohere()
    r = _reranker(monkeypatch, client=fake)
    out = r.rerank("qualified research", _candidates(), top_k=3)

    assert [c.chunk.chunk_id for c in out] == ["c3", "c2", "c1"]
    assert fake.calls[0]["model"] == "rerank-v3.5"
    assert fake.calls[0]["n_docs"] == 3
    # Score is replaced by the cross-encoder's absolute relevance, not blended.
    assert out[0].score == pytest.approx(0.3)
    assert out[0].rerank_score == pytest.approx(0.3)


def test_rerank_preserves_cosine_for_the_abstention_gate(monkeypatch):
    """Reranking must not disturb the only absolute relevance signal."""
    r = _reranker(monkeypatch, client=_FakeCohere())
    cands = _candidates()
    before = {c.chunk.chunk_id: c.cosine for c in cands}
    out = r.rerank("q", cands, top_k=3)
    assert {c.chunk.chunk_id: c.cosine for c in out} == before


def test_rerank_preserves_rrf_score_for_diagnostics(monkeypatch):
    r = _reranker(monkeypatch, client=_FakeCohere())
    out = r.rerank("q", _candidates(), top_k=3)
    top = next(c for c in out if c.chunk.chunk_id == "c1")
    assert top.rrf_score == pytest.approx(0.032)


def test_top_k_truncates(monkeypatch):
    r = _reranker(monkeypatch, client=_FakeCohere())
    assert len(r.rerank("q", _candidates(), top_k=2)) == 2


def test_cohere_failure_falls_back_to_rrf_order_not_the_llm(monkeypatch):
    """An outage must not silently divert traffic to the pricier LLM path."""
    r = _reranker(monkeypatch, client=_ExplodingCohere())

    def _boom(*a, **k):
        raise AssertionError("LLM reranker must not be used as a fallback")

    monkeypatch.setattr(r, "_llm_rerank", _boom)

    out = r.rerank("q", _candidates(), top_k=3)
    assert [c.chunk.chunk_id for c in out] == ["c1", "c2", "c3"]  # RRF order
    assert out[0].rerank_score is None  # nothing claimed to have reranked


def test_missing_api_key_degrades_to_none(monkeypatch):
    """Unset credentials must degrade to RRF order, not look like a live reranker.

    Settings fall back to the .env file, so clearing the environment variable is
    not enough to simulate an unconfigured deployment -- inject the settings.
    """
    from finance_rag.config import Settings
    from finance_rag.ranking import reranker as reranker_module

    keyless = Settings(RERANK_PROVIDER="cohere", COHERE_API_KEY="", _env_file=None)
    monkeypatch.setattr(reranker_module, "get_settings", lambda: keyless)

    r = reranker_module.Reranker()
    assert r._cohere is None
    assert r.active_provider == "none"
    out = r.rerank("q", _candidates(), top_k=3)
    assert [c.chunk.chunk_id for c in out] == ["c1", "c2", "c3"]


def test_provider_none_is_a_passthrough(monkeypatch):
    r = _reranker(monkeypatch, provider="none", client=None)
    cands = _candidates()
    out = r.rerank("q", cands, top_k=3)
    assert [c.chunk.chunk_id for c in out] == ["c1", "c2", "c3"]
    assert all(c.rerank_score is None for c in out)


def test_empty_candidates_short_circuit(monkeypatch):
    fake = _FakeCohere()
    r = _reranker(monkeypatch, client=fake)
    assert r.rerank("q", [], top_k=5) == []
    assert fake.calls == []  # no API call, no spend


def test_invalid_provider_is_rejected_at_startup(monkeypatch):
    from finance_rag.config import Settings

    monkeypatch.setenv("RERANK_PROVIDER", "gpt5-turbo-rerank")
    with pytest.raises(ValueError, match="RERANK_PROVIDER"):
        Settings()
