"""Server-free checks on the store's SQL.

These cannot validate Postgres semantics, but they do catch the mistake that
hand-written SQL invites most often: a ``:param`` in the statement that nobody
binds, or a bound value the statement never references. Both fail at runtime
only when the query is first executed, which for a rarely-hit path can be much
later than it should be.
"""

from __future__ import annotations

import re
from contextlib import contextmanager

import pytest

from finance_rag.models import Chunk, DocumentMeta

# ``:word`` but not ``::cast`` -- pgvector/jsonb casts are everywhere in this SQL.
PARAM_RE = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")


class _RecordingConnection:
    def __init__(self, calls):
        self._calls = calls

    def execute(self, statement, params=None):
        self._calls.append((str(statement), params))
        return _EmptyResult()


class _EmptyResult:
    def mappings(self):
        return self

    def __iter__(self):
        return iter(())

    def one(self):
        return {}

    def scalar(self):
        return None


@pytest.fixture
def recorded(monkeypatch):
    """Swap the store's connection scope for a recorder."""
    calls: list[tuple[str, object]] = []

    @contextmanager
    def fake_connection():
        yield _RecordingConnection(calls)

    monkeypatch.setattr("finance_rag.store.pgvector_store.connection", fake_connection)
    return calls


def _assert_params_balance(sql: str, params):
    referenced = set(PARAM_RE.findall(sql))
    if params is None:
        bound = set()
    elif isinstance(params, list):
        bound = set(params[0].keys()) if params else set()
    else:
        bound = set(params.keys())

    assert not (referenced - bound), f"unbound params in SQL: {sorted(referenced - bound)}"
    assert not (bound - referenced), f"bound but unused params: {sorted(bound - referenced)}"


@pytest.fixture
def store():
    from finance_rag.store import PgVectorStore

    return PgVectorStore()


def test_hybrid_rrf_params_balance(store, recorded):
    store.hybrid_search_rrf([0.1] * store.embedding_dim, "four-part test", top_k=5)
    sql, params = recorded[-1]
    _assert_params_balance(sql, params)


def test_hybrid_rrf_sql_implements_the_formula(store, recorded):
    """The SQL must fuse 1/(k+rank) per ranker and keep cosine separate."""
    store.hybrid_search_rrf([0.1] * store.embedding_dim, "q", top_k=5)
    sql, _ = recorded[-1]
    assert "1.0 / (:k + d.rank)" in sql
    assert "1.0 / (:k + s.rank)" in sql
    assert "ROW_NUMBER() OVER" in sql
    assert "FULL OUTER JOIN" in sql  # a doc found by either ranker must survive
    assert "AS cosine" in sql  # absolute signal returned alongside the fusion


def test_vector_search_params_balance(store, recorded):
    store.vector_search([0.1] * store.embedding_dim, top_k=5)
    _assert_params_balance(*recorded[-1])


def test_fulltext_search_params_balance(store, recorded):
    store.fulltext_search("cost segregation", top_k=5)
    _assert_params_balance(*recorded[-1])


def test_graph_expand_params_balance(store, recorded):
    store.graph_expand(["a", "b"], limit=4)
    sql, params = recorded[-1]
    _assert_params_balance(sql, params)
    assert "<> ALL(" in sql  # seeds excluded from their own expansion


def test_fetch_parent_context_params_balance(store, recorded):
    store.fetch_parent_context(["p1"])
    _assert_params_balance(*recorded[-1])


def test_upsert_document_params_balance(store, recorded):
    store.upsert_document(
        DocumentMeta(doc_id="d1", source="s", title="t", service_line="R&D Tax Credit")
    )
    _assert_params_balance(*recorded[-1])


def test_upsert_chunks_params_balance(store, recorded):
    store.upsert_chunks(
        [
            Chunk(
                chunk_id="c1",
                doc_id="d1",
                text="body",
                index=0,
                tokens=2,
                metadata={"level": "child"},
                embedding=[0.1] * store.embedding_dim,
            )
        ]
    )
    _assert_params_balance(*recorded[-1])


def test_link_entities_params_balance(store, recorded):
    store.link_entities("c1", ["IRC 41", "LIFO"])
    _assert_params_balance(*recorded[-1])


def test_empty_inputs_short_circuit_without_touching_the_database(store, recorded):
    assert store.graph_expand([], limit=4) == []
    assert store.fetch_parent_context([]) == []
    assert store.upsert_chunks([]) == 0
    store.link_entities("c1", [])
    assert recorded == []


def test_chunk_without_embedding_is_skipped(store, recorded):
    unembedded = Chunk(chunk_id="c9", doc_id="d1", text="x", index=0, tokens=1)
    assert store.upsert_chunks([unembedded]) == 0
    assert recorded == []


def test_document_effective_date_rejects_non_iso_values(store, recorded):
    """Corpus metadata carries values like "TY2024"; a DATE cast would abort."""
    store.upsert_document(
        DocumentMeta(doc_id="d2", source="s", title="t", effective_date="TY2024")
    )
    _, params = recorded[-1]
    assert params["effective_date"] is None

    store.upsert_document(
        DocumentMeta(doc_id="d3", source="s", title="t", effective_date="2024-01-31")
    )
    _, params = recorded[-1]
    assert params["effective_date"] is not None
