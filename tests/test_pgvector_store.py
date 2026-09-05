"""Integration tests for the Postgres/pgvector store.

Skipped unless a database is reachable, so the unit suite still runs anywhere:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/test_pgvector_store.py
"""

from __future__ import annotations

import uuid

import pytest

from finance_rag.config import get_settings
from finance_rag.models import Chunk, DocumentMeta

pytestmark = pytest.mark.integration


def _database_available() -> bool:
    try:
        from finance_rag.db import healthcheck

        return healthcheck()
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not _database_available(), reason="Postgres not reachable; run docker compose up -d postgres"
)


def _vec(seed: float, dim: int) -> list[float]:
    """Deterministic unit-ish vector so cosine ordering is predictable."""
    return [seed] + [0.0] * (dim - 1)


@pytest.fixture
def store():
    from finance_rag.store import PgVectorStore

    return PgVectorStore()


@pytest.fixture
def seeded(store):
    """Insert a throwaway document with two child chunks, then clean up."""
    dim = get_settings().openai_embedding_dimensions
    doc_id = f"test_{uuid.uuid4().hex[:12]}"
    meta = DocumentMeta(
        doc_id=doc_id,
        source="tests",
        title="R&D Tax Credit Test Doc",
        service_line="R&D Tax Credit",
        jurisdiction="USA",
        doc_type="md",
    )
    store.upsert_document(meta)

    chunks = [
        Chunk(
            chunk_id=f"{doc_id}_a",
            doc_id=doc_id,
            text="The four-part test governs qualified research expenses under IRC 41.",
            index=0,
            tokens=12,
            section="Four-part test",
            metadata={"level": "child", "title": meta.title, "service_line": meta.service_line},
            embedding=_vec(1.0, dim),
        ),
        Chunk(
            chunk_id=f"{doc_id}_b",
            doc_id=doc_id,
            text="Cost segregation accelerates depreciation to improve cash flow.",
            index=1,
            tokens=10,
            section="Cost segregation",
            metadata={"level": "child", "title": meta.title, "service_line": meta.service_line},
            embedding=_vec(-1.0, dim),
        ),
    ]
    store.upsert_chunks(chunks)
    yield doc_id, chunks, dim

    from sqlalchemy import text

    from finance_rag.db import connection

    with connection() as conn:
        conn.execute(text("DELETE FROM documents WHERE doc_id = :d"), {"d": doc_id})


@requires_db
def test_ensure_schema_accepts_configured_dimensions(store):
    store.ensure_schema()


@requires_db
def test_upsert_is_idempotent(store, seeded):
    _, chunks, _ = seeded
    assert store.upsert_chunks(chunks) == 2
    assert store.upsert_chunks(chunks) == 2  # ON CONFLICT DO UPDATE, not a duplicate


@requires_db
def test_upsert_rejects_wrong_dimension(store, seeded):
    doc_id, _, dim = seeded
    bad = Chunk(
        chunk_id=f"{doc_id}_bad",
        doc_id=doc_id,
        text="wrong width",
        index=2,
        tokens=2,
        metadata={"level": "child"},
        embedding=[0.1] * (dim + 1),
    )
    with pytest.raises(ValueError, match="dimensions"):
        store.upsert_chunks([bad])


@requires_db
def test_hybrid_rrf_returns_both_ordinal_and_absolute_signals(store, seeded):
    doc_id, _, dim = seeded
    rows = store.hybrid_search_rrf(
        embedding=_vec(1.0, dim),
        query_text="four-part test qualified research",
        top_k=10,
    )
    hit = next(r for r in rows if r["chunk_id"] == f"{doc_id}_a")

    # Ordinal fusion score, on the 1/(k+rank) scale.
    assert 0 < float(hit["rrf_score"]) <= 2 / 61
    # Absolute similarity, independent of the candidate set.
    assert float(hit["cosine"]) == pytest.approx(1.0, abs=1e-6)
    # The opposing vector must not look relevant just because it ranked.
    opposite = next((r for r in rows if r["chunk_id"] == f"{doc_id}_b"), None)
    if opposite is not None:
        assert float(opposite["cosine"]) < float(hit["cosine"])


@requires_db
def test_hybrid_rrf_survives_a_query_with_no_lexical_match(store, seeded):
    doc_id, _, dim = seeded
    rows = store.hybrid_search_rrf(
        embedding=_vec(1.0, dim),
        query_text="zzzzqqqq nonexistent lexical token",
        top_k=10,
    )
    ids = {r["chunk_id"] for r in rows}
    assert f"{doc_id}_a" in ids  # dense ranker alone still contributes


@requires_db
def test_service_line_filter_applies(store, seeded):
    _, _, dim = seeded
    rows = store.hybrid_search_rrf(
        embedding=_vec(1.0, dim),
        query_text="four-part test",
        top_k=10,
        service_line="Nonexistent Service Line",
    )
    assert rows == []


@requires_db
def test_entity_links_drive_graph_expansion(store, seeded):
    doc_id, _, _ = seeded
    store.link_entities(f"{doc_id}_a", ["IRC 41", "R&D Tax Credit"])
    store.link_entities(f"{doc_id}_b", ["R&D Tax Credit"])

    expanded = store.graph_expand([f"{doc_id}_a"], limit=5)
    ids = {r["chunk_id"] for r in expanded}
    assert f"{doc_id}_b" in ids  # reached via the shared entity
    assert f"{doc_id}_a" not in ids  # seeds are excluded from their own expansion


@requires_db
def test_link_entities_is_idempotent(store, seeded):
    doc_id, _, _ = seeded
    store.link_entities(f"{doc_id}_a", ["IRC 41"])
    store.link_entities(f"{doc_id}_a", ["IRC 41"])
    expanded = store.graph_expand([f"{doc_id}_a"], limit=5)
    assert all(int(r["score"]) >= 1 for r in expanded)
