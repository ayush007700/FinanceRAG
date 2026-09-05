"""Multi-agent orchestration: routing, the critic cycle, and memory.

These pin the behaviours that make this a graph rather than a pipeline. All
model calls are stubbed, so the suite runs without credentials or spend.
"""

from __future__ import annotations

import json

import pytest

from finance_rag.agent.roles import Critic, Supervisor, WebSearchAgent
from finance_rag.config import get_settings
from finance_rag.models import Chunk, RetrievedChunk


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _chunks(*ids: str) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk=Chunk(chunk_id=c, doc_id="d", text=f"passage {c}", index=i, tokens=4),
            score=0.03,
            cosine=0.7,
        )
        for i, c in enumerate(ids)
    ]


class _FakeOpenAI:
    def __init__(self, payload=None, raises=None):
        self.payload = payload or {}
        self.raises = raises
        self.calls: list[dict] = []
        outer = self

        class _C:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer.raises:
                    raise outer.raises
                content = json.dumps(outer.payload)
                msg = type("M", (), {"content": content})()
                return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

        self.chat = type("Chat", (), {"completions": _C()})()


# ------------------------------------------------------------------ supervisor


def test_supervisor_routes_to_corpus_by_default():
    s = Supervisor(client=_FakeOpenAI({"route": "corpus", "service_line": "R&D Tax Credit"}))
    plan = s.plan("What is the four-part test?")
    assert plan.route == "corpus"
    assert plan.service_line == "R&D Tax Credit"


def test_supervisor_uses_the_cheap_model():
    fake = _FakeOpenAI({"route": "corpus"})
    Supervisor(client=fake).plan("q")
    assert fake.calls[0]["model"] == get_settings().openai_fast_model


def test_unknown_route_falls_back_to_corpus():
    s = Supervisor(client=_FakeOpenAI({"route": "teleport"}))
    assert s.plan("q").route == "corpus"


def test_router_failure_defaults_to_corpus():
    """The corpus is the safe path: its sources are curated and citable."""
    s = Supervisor(client=_FakeOpenAI(raises=RuntimeError("router down")))
    plan = s.plan("q")
    assert plan.route == "corpus"
    assert "defaulted" in plan.rationale


def test_web_route_is_refused_when_web_search_is_disabled(monkeypatch):
    """Never route to a capability that is switched off."""
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    get_settings.cache_clear()
    s = Supervisor(client=_FakeOpenAI({"route": "web"}))
    assert s.plan("what changed in the 2026 rules?").route == "corpus"


def test_web_route_survives_when_enabled(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    get_settings.cache_clear()
    s = Supervisor(client=_FakeOpenAI({"route": "web"}))
    assert s.plan("q").route == "web"


# ----------------------------------------------------------------- web search


def test_web_search_is_disabled_without_a_key(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    get_settings.cache_clear()
    assert WebSearchAgent().search("q") == []


class _FakeHTTP:
    def __init__(self, payload, status_ok=True):
        self.payload = payload
        self.status_ok = status_ok

    def post(self, *a, **kw):
        payload = self.payload
        ok = self.status_ok

        class _R:
            def raise_for_status(self):
                if not ok:
                    raise RuntimeError("502")

            def json(self):
                return payload

        return _R()


def _web_agent(monkeypatch, http):
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    get_settings.cache_clear()
    return WebSearchAgent(http_client=http)


def test_web_results_become_citable_chunks(monkeypatch):
    """Web claims must travel the same citation path as corpus claims.

    Otherwise an open-web claim reaches the user unverified while corpus claims
    are checked -- a hole straight through the compliance story.
    """
    http = _FakeHTTP(
        {"results": [{"url": "https://irs.gov/x", "title": "IRS", "content": "Rate is 14%.",
                      "score": 0.9}]}
    )
    out = _web_agent(monkeypatch, http).search("2026 rate")
    assert len(out) == 1
    assert out[0].chunk.chunk_id.startswith("web-")
    assert out[0].chunk.metadata["modality"] == "web"
    assert out[0].chunk.metadata["source"] == "https://irs.gov/x"
    assert out[0].chunk.metadata["fetched_at"]


def test_web_result_never_populates_cosine(monkeypatch):
    """The abstention gate reads cosine; a provider score is a different scale."""
    http = _FakeHTTP({"results": [{"url": "u", "content": "text", "score": 0.95}]})
    out = _web_agent(monkeypatch, http).search("q")
    assert out[0].cosine == 0.0
    assert out[0].rerank_score == pytest.approx(0.95)


def test_web_search_failure_degrades_to_empty(monkeypatch):
    out = _web_agent(monkeypatch, _FakeHTTP({}, status_ok=False)).search("q")
    assert out == []


def test_web_results_are_stable_across_calls(monkeypatch):
    """Ids derive from the URL, so the same page keeps the same citation id."""
    http = _FakeHTTP({"results": [{"url": "https://a.test/p", "content": "x", "score": 0.5}]})
    agent = _web_agent(monkeypatch, http)
    assert agent.search("q")[0].chunk.chunk_id == agent.search("q")[0].chunk.chunk_id


# --------------------------------------------------------------------- critic


def test_critic_approves_a_supported_answer():
    c = Critic(client=_FakeOpenAI({"approved": True}))
    assert c.review("q", "Answer [c1].", _chunks("c1")).approved


def test_critic_rejects_and_can_suggest_a_new_query():
    c = Critic(client=_FakeOpenAI({
        "approved": False,
        "reasons": ["unsupported figure"],
        "suggested_query": "qualified research expenses categories",
    }))
    verdict = c.review("q", "The rate is 14%.", _chunks("c1"))
    assert verdict.approved is False
    assert verdict.reasons == ["unsupported figure"]
    assert verdict.suggested_query == "qualified research expenses categories"


def test_empty_answer_is_rejected_without_a_model_call():
    fake = _FakeOpenAI({"approved": True})
    verdict = Critic(client=fake).review("q", "   ", _chunks("c1"))
    assert verdict.approved is False
    assert fake.calls == []


def test_critic_failure_fails_open():
    """Deterministic citation checks still run downstream, so lose a layer, not the service."""
    c = Critic(client=_FakeOpenAI(raises=RuntimeError("down")))
    verdict = c.review("q", "answer", _chunks("c1"))
    assert verdict.approved is True
    assert verdict.checked is False


def test_critic_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CRITIC_ENABLED", "false")
    get_settings.cache_clear()
    fake = _FakeOpenAI({"approved": False})
    verdict = Critic(client=fake).review("q", "answer", _chunks("c1"))
    assert verdict.approved is True
    assert fake.calls == []


# ------------------------------------------------------------ graph topology


def _agent():
    from finance_rag.agent.orchestrator import MultiAgentRAG

    return MultiAgentRAG.__new__(MultiAgentRAG)


def test_graph_has_all_six_roles():
    import inspect

    from finance_rag.agent.orchestrator import MultiAgentRAG

    src = inspect.getsource(MultiAgentRAG._build_graph)
    nodes = {line.split('"')[1] for line in src.splitlines() if "add_node" in line}
    assert nodes == {
        "supervisor", "researcher", "web_search", "answerability", "analyst",
        "critic", "compliance",
    }


def test_critic_rejection_loops_back_to_retrieval():
    """The cycle is what makes this a graph rather than a DAG."""
    a = _agent()
    a.settings = get_settings()
    assert a._after_critic({"_critic_approved": False, "critic_attempts": 1}) == "researcher"


def test_critic_loop_is_bounded(monkeypatch):
    """An unbounded self-correction loop is an unbounded bill."""
    monkeypatch.setenv("CRITIC_MAX_RETRIES", "1")
    get_settings.cache_clear()
    a = _agent()
    a.settings = get_settings()
    assert a._after_critic({"_critic_approved": False, "critic_attempts": 2}) == "compliance"


def test_approved_draft_goes_to_compliance():
    a = _agent()
    a.settings = get_settings()
    assert a._after_critic({"_critic_approved": True, "critic_attempts": 0}) == "compliance"


def test_blocked_input_skips_straight_to_compliance():
    a = _agent()
    assert a._route_from_supervisor({"allowed": False}) == "compliance"


def test_there_is_no_route_that_refuses_before_seeing_evidence():
    """Regression: a "direct" route refused without retrieving anything.

    It classified "What values guide Source Advisors client work?" as
    conversational and returned a canned refusal, while retrieval scored 0.684
    on the passage stating those values. The router decides before seeing
    passages; the answerability gate decides after, so the gate is strictly
    better informed and is the only place a refusal belongs.
    """
    from finance_rag.agent.roles import _ROUTES

    assert "direct" not in _ROUTES
    a = _agent()
    assert a._route_from_supervisor({"allowed": True, "route": "corpus"}) == "researcher"
    # An unrecognised route must still retrieve rather than refuse.
    assert a._route_from_supervisor({"allowed": True, "route": "chitchat"}) == "researcher"


def test_both_route_chains_corpus_into_web():
    a = _agent()
    assert a._after_research({"route": "both"}) == "web_search"
    assert a._after_research({"route": "corpus"}) == "answerability"


def test_unanswerable_skips_generation():
    a = _agent()
    assert a._after_answerability({"answerable": False}) == "compliance"
    assert a._after_answerability({"answerable": True}) == "analyst"


def test_image_caption_reaches_the_retrieval_query():
    from finance_rag.agent.orchestrator import _search_query

    assert _search_query("what is this?", None) == "what is this?"
    assert "Image: a depreciation schedule" in _search_query(
        "what is this?", "a depreciation schedule"
    )


# --------------------------------------------------------- web content cleaning


def test_site_chrome_is_stripped_from_page_text():
    """Regression: raw_content leads with banners, so the head is navigation.

    Truncating it captured .gov notices and none of the article, which made the
    answerability gate report that an IRS page about a deduction never mentioned
    the deduction.
    """
    from finance_rag.agent.roles import clean_web_text

    raw = (
        "![](/img/us_flag.png)\n"
        "An official website of the United States government\n"
        "Here's how you know\n"
        "Official websites use .gov\n"
        "Secure .gov websites use HTTPS\n"
        "Skip to main content\n"
        "The Section 179 deduction limit for tax year 2026 is $2,500,000 for "
        "qualifying property placed in service during the year.\n"
    )
    out = clean_web_text(raw)
    assert "official website" not in out.lower()
    assert "Skip to main content" not in out
    assert "$2,500,000" in out
    assert "Section 179" in out


def test_markdown_images_and_links_are_flattened():
    from finance_rag.agent.roles import clean_web_text

    raw = "![alt](/x.png) See [the guidance page](https://irs.gov/x) for the full rules here."
    out = clean_web_text(raw)
    assert "/x.png" not in out
    assert "https://irs.gov/x" not in out
    assert "the guidance page" in out


def test_provider_snippet_leads_the_cleaned_text():
    """The snippet is relevance-selected for the query, so it goes first."""
    from finance_rag.agent.roles import clean_web_text

    out = clean_web_text(
        raw="Some general page body text that continues on for a while here.",
        snippet="The rate is 14 percent.",
    )
    assert out.startswith("The rate is 14 percent.")


def test_cleaning_respects_the_character_cap():
    from finance_rag.agent.roles import clean_web_text

    out = clean_web_text("word " * 5000, limit=500)
    assert len(out) <= 500


def test_empty_page_yields_empty_text():
    from finance_rag.agent.roles import clean_web_text

    assert clean_web_text("", "") == ""


def test_news_topic_sets_a_recency_window(monkeypatch):
    """Stale tax guidance is the specific failure the news topic avoids."""
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    get_settings.cache_clear()

    captured = {}

    class _HTTP:
        def post(self, url, json=None, timeout=None):
            captured.update(json or {})

            class _R:
                def raise_for_status(self): ...
                def json(self): return {"results": []}

            return _R()

    WebSearchAgent(http_client=_HTTP()).search("2026 rates", freshness=True)
    assert captured["topic"] == "news"
    assert captured["days"] == get_settings().web_search_days
    assert captured["include_raw_content"] is True
    assert "irs.gov" in captured["include_domains"]


def test_general_topic_sends_no_recency_window(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    get_settings.cache_clear()

    captured = {}

    class _HTTP:
        def post(self, url, json=None, timeout=None):
            captured.update(json or {})

            class _R:
                def raise_for_status(self): ...
                def json(self): return {"results": []}

            return _R()

    WebSearchAgent(http_client=_HTTP()).search("what is cost segregation", freshness=False)
    assert captured["topic"] == "general"
    assert "days" not in captured


def test_refusal_names_the_sources_actually_searched():
    """Saying the firm's material lacks an answer misleads when the web was searched."""
    from finance_rag.guardrails.answerability import AnswerabilityVerdict

    v = AnswerabilityVerdict(answerable=False, missing="the 2026 limit")
    v.sources_consulted = "current web sources"
    assert "current web sources" in v.refusal_message
    assert "Source Advisors" not in v.refusal_message


# ------------------------------------------------------- checkpointer config


def _agent_with_checkpointer(recorder: dict):
    """An orchestrator whose graph only records what it was invoked with.

    Bypasses __init__ so the test costs no model calls and needs no database:
    what is under test is the config assembled in `ask`, not the graph.
    """
    from finance_rag.config import get_settings

    agent = _agent()
    agent.settings = get_settings()
    agent.checkpointer = object()  # any non-None checkpointer
    agent._on_stage = None

    class _Graph:
        def invoke(self, state, config):
            recorder["state"] = state
            recorder["config"] = config
            return {"answer": "ok", "confidence": 0.5}

    agent.graph = _Graph()
    return agent


def test_ask_without_a_thread_id_still_supplies_one_to_the_checkpointer():
    """LangGraph rejects a checkpointed run with no thread_id, and the API
    leaves thread_id optional while always attaching a checkpointer -- so every
    single-shot question through /v1/ask was a 500."""
    recorder: dict = {}
    _agent_with_checkpointer(recorder).ask("what is cost segregation")

    assert recorder["config"]["configurable"]["thread_id"]


def test_a_generated_thread_id_is_not_presented_as_a_conversation():
    """The synthetic id is how the request finds its own checkpoint. Recording
    it as the caller's thread would put a resumable-looking id nobody holds into
    the audit trail."""
    recorder: dict = {}
    _agent_with_checkpointer(recorder).ask("what is cost segregation")

    assert recorder["state"]["thread_id"] is None


def test_a_supplied_thread_id_is_used_verbatim():
    """Passing the same id across requests is what resumes a conversation."""
    recorder: dict = {}
    _agent_with_checkpointer(recorder).ask("and for 2023?", thread_id="thread-7")

    assert recorder["config"]["configurable"]["thread_id"] == "thread-7"
    assert recorder["state"]["thread_id"] == "thread-7"


def test_each_single_shot_question_gets_its_own_thread():
    """Sharing one generated id would leak conversation memory between
    unrelated callers."""
    a, b = {}, {}
    _agent_with_checkpointer(a).ask("first question")
    _agent_with_checkpointer(b).ask("second question")

    assert a["config"]["configurable"]["thread_id"] != b["config"]["configurable"]["thread_id"]
