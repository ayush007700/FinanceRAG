"""Initial pgvector schema: documents, chunks, entities, chunk_entities.

Replaces the Neo4j knowledge store. Vector search, full-text search and the
entity adjacency that previously required a graph database all live here.

Revision ID: 0001
Revises:
Create Date: 2026-09-01
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Embedding width is fixed in the schema on purpose: pgvector cannot index a
# `vector` wider than 2000 dimensions with HNSW, and changing width invalidates
# every stored embedding. Switching models therefore requires a new migration
# plus a full re-index, which is exactly the friction we want.
EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE documents (
            doc_id          TEXT PRIMARY KEY,
            source          TEXT NOT NULL,
            title           TEXT NOT NULL,
            service_line    TEXT,
            jurisdiction    TEXT,
            doc_type        TEXT NOT NULL DEFAULT 'policy',
            effective_date  DATE,
            tags            TEXT[] NOT NULL DEFAULT '{}',
            extra           JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX documents_service_line_idx ON documents (service_line)")
    op.execute("CREATE INDEX documents_effective_date_idx ON documents (effective_date)")

    op.execute(
        f"""
        CREATE TABLE chunks (
            chunk_id        TEXT PRIMARY KEY,
            doc_id          TEXT NOT NULL
                                REFERENCES documents (doc_id) ON DELETE CASCADE,
            parent_id       TEXT
                                REFERENCES chunks (chunk_id) ON DELETE SET NULL
                                DEFERRABLE INITIALLY DEFERRED,
            text            TEXT NOT NULL,
            chunk_index     INTEGER NOT NULL DEFAULT 0,
            tokens          INTEGER NOT NULL DEFAULT 0,
            section         TEXT,
            level           TEXT NOT NULL DEFAULT 'child',
            title           TEXT,
            source          TEXT,
            service_line    TEXT,
            jurisdiction    TEXT,
            modality        TEXT NOT NULL DEFAULT 'text',
            metadata        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            embedding       vector({EMBEDDING_DIM}),
            tsv             tsvector GENERATED ALWAYS AS (
                                setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                                setweight(to_tsvector('english', coalesce(section, '')), 'B') ||
                                setweight(to_tsvector('english', coalesce(text, '')), 'C')
                            ) STORED,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # Dense ranker. Partial on level='child' because parents are context-only and
    # are never vector-searched -- this makes the level filter free rather than
    # forcing HNSW to over-fetch and post-filter.
    op.execute(
        """
        CREATE INDEX chunks_embedding_hnsw_idx ON chunks
        USING hnsw (embedding vector_cosine_ops)
        WHERE level = 'child'
        """
    )
    # Sparse ranker.
    op.execute("CREATE INDEX chunks_tsv_gin_idx ON chunks USING gin (tsv)")

    op.execute("CREATE INDEX chunks_doc_id_idx ON chunks (doc_id)")
    op.execute("CREATE INDEX chunks_parent_id_idx ON chunks (parent_id)")
    op.execute("CREATE INDEX chunks_level_service_idx ON chunks (level, service_line)")

    # Entity adjacency: the Neo4j (:Chunk)-[:MENTIONS]->(:Entity) edge, ported.
    op.execute(
        """
        CREATE TABLE entities (
            entity_id   BIGSERIAL PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL DEFAULT 'concept',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE chunk_entities (
            chunk_id   TEXT   NOT NULL REFERENCES chunks (chunk_id) ON DELETE CASCADE,
            entity_id  BIGINT NOT NULL REFERENCES entities (entity_id) ON DELETE CASCADE,
            PRIMARY KEY (chunk_id, entity_id)
        )
        """
    )
    # Reverse lookup drives multi-hop expansion (entity -> co-mentioning chunks).
    op.execute("CREATE INDEX chunk_entities_entity_idx ON chunk_entities (entity_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chunk_entities")
    op.execute("DROP TABLE IF EXISTS entities")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS documents")
