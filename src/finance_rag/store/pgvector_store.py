"""Postgres + pgvector knowledge store.

Single-store replacement for the previous Neo4j backend. Postgres covers all
three jobs the graph database was doing:

  * dense retrieval   -> ``vector`` column + HNSW index (``<=>`` cosine distance)
  * sparse retrieval  -> generated ``tsvector`` column + GIN index
  * entity expansion  -> ``chunk_entities`` adjacency table

Fusion is Reciprocal Rank Fusion executed inside a single SQL statement, which
replaces the previous two-query + Python min-max blend.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from sqlalchemy import text

from finance_rag.config import get_settings
from finance_rag.db import connection, to_vector_literal
from finance_rag.logging_setup import get_logger
from finance_rag.models import Chunk, DocumentMeta

logger = get_logger(__name__)

# Columns every retrieval path returns, so rows map to Chunk uniformly.
_CHUNK_COLUMNS = """
    c.chunk_id, c.doc_id, c.text, c.section, c.title, c.source,
    c.service_line, c.jurisdiction, c.parent_id, c.modality,
    c.chunk_index, c.tokens, c.metadata
"""

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Postgres ships no "match any term" query builder: plainto_tsquery and
# websearch_to_tsquery both AND every lexeme, so a natural-language question only
# matches a chunk containing *every* word. For real questions that means nothing
# matches -- "What is the four-part test for R&D tax credit qualification?"
# becomes a nine-term conjunction returning zero rows out of 291, which silently
# reduced hybrid retrieval to dense-only.
#
# Lexemes are therefore extracted and re-joined with the OR operator. NULLIF
# guards a query that reduces to no lexemes at all (stopwords only): to_tsquery
# raises on an empty string, whereas NULL simply matches nothing.
_OR_TSQUERY = (
    "to_tsquery('english', NULLIF(array_to_string("
    "tsvector_to_array(to_tsvector('english', :qtext)), ' | '), ''))"
)

# ts_rank_cd has no IDF, so under a pure OR query long chunks that merely repeat
# common terms ("tax", "credit") outrank the passage that answers the question.
# 1|16 divides by both document length and unique-word count, demoting exactly
# that boilerplate: on the four-part-test query it moves the correct passage from
# rank 6 to rank 1.
_RANK_NORMALIZATION = 1 | 16


def _coerce_date(value: Any) -> date | None:
    """Accept only unambiguous ISO dates; anything else is dropped.

    Corpus metadata is user-supplied and frequently carries values like
    "TY2024" that would abort the whole insert on a DATE cast.
    """
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE_RE.match(value.strip()):
        return date.fromisoformat(value.strip())
    return None


def _scope_predicates(as_of: date | None, alias: str = "") -> str:
    """WHERE fragment applying tenancy and, optionally, effective dating.

    ``org_id`` is an equality filter rather than an advisory signal: cross-tenant
    leakage is the one retrieval failure that cannot be walked back, so unlike
    ``service_line`` it is never softened into a ranking hint.

    Effective dating is opt-in because a corpus whose documents carry no dates
    would otherwise retrieve nothing at all.
    """
    prefix = f"{alias}." if alias else ""
    clauses = [f"{prefix}org_id = :org_id"]
    if as_of is not None:
        clauses.append(f"({prefix}effective_date IS NULL OR {prefix}effective_date <= :as_of)")
        clauses.append(f"({prefix}superseded_date IS NULL OR {prefix}superseded_date > :as_of)")
    return " AND ".join(clauses)


class PgVectorStore:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedding_dim = self.settings.openai_embedding_dimensions

    # ---------------------------------------------------------------- schema

    def ensure_schema(self) -> None:
        """Verify migrations have run and the embedding width matches config."""
        with connection() as conn:
            exists = conn.execute(
                text("SELECT to_regclass('public.chunks') IS NOT NULL")
            ).scalar()
            if not exists:
                raise RuntimeError(
                    "Database schema is missing. Run migrations first:\n"
                    "  alembic upgrade head"
                )
            actual_dim = conn.execute(
                text(
                    """
                    SELECT atttypmod
                    FROM pg_attribute
                    WHERE attrelid = 'public.chunks'::regclass AND attname = 'embedding'
                    """
                )
            ).scalar()
            if actual_dim and actual_dim > 0 and actual_dim != self.embedding_dim:
                raise RuntimeError(
                    f"Embedding width mismatch: chunks.embedding is vector({actual_dim}) "
                    f"but OPENAI_EMBEDDING_DIMENSIONS={self.embedding_dim}. "
                    "Changing width requires a migration and a full re-index."
                )
        logger.info("pg_schema_ready", embedding_dim=self.embedding_dim)

    # --------------------------------------------------------------- writing

    def upsert_document(self, meta: DocumentMeta, org_id: str | None = None) -> None:
        with connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO documents (
                        doc_id, source, title, service_line, jurisdiction,
                        doc_type, effective_date, tags, extra, org_id
                    )
                    VALUES (
                        :doc_id, :source, :title, :service_line, :jurisdiction,
                        :doc_type, :effective_date, :tags, CAST(:extra AS jsonb),
                        :org_id
                    )
                    ON CONFLICT (doc_id) DO UPDATE SET
                        source         = EXCLUDED.source,
                        title          = EXCLUDED.title,
                        service_line   = EXCLUDED.service_line,
                        jurisdiction   = EXCLUDED.jurisdiction,
                        doc_type       = EXCLUDED.doc_type,
                        effective_date = EXCLUDED.effective_date,
                        tags           = EXCLUDED.tags,
                        extra          = EXCLUDED.extra,
                        org_id         = EXCLUDED.org_id,
                        updated_at     = now()
                    """
                ),
                {
                    "doc_id": meta.doc_id,
                    "source": meta.source,
                    "title": meta.title,
                    "service_line": meta.service_line,
                    "jurisdiction": meta.jurisdiction,
                    "doc_type": meta.doc_type,
                    "effective_date": _coerce_date(meta.effective_date),
                    "tags": list(meta.tags or []),
                    "extra": json.dumps(meta.extra or {}, default=str),
                    "org_id": org_id or self.settings.default_org_id,
                },
            )

    def upsert_chunks(self, chunks: list[Chunk], org_id: str | None = None) -> int:
        org = org_id or self.settings.default_org_id
        rows = []
        for chunk in chunks:
            if not chunk.embedding:
                continue
            if len(chunk.embedding) != self.embedding_dim:
                raise ValueError(
                    f"Chunk {chunk.chunk_id} has {len(chunk.embedding)} dimensions, "
                    f"expected {self.embedding_dim}"
                )
            meta = chunk.metadata or {}
            rows.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "parent_id": chunk.parent_id,
                    "text": chunk.text,
                    "chunk_index": chunk.index,
                    "tokens": chunk.tokens,
                    "section": chunk.section,
                    "level": meta.get("level", "child"),
                    "title": meta.get("title"),
                    "source": meta.get("source"),
                    "service_line": meta.get("service_line"),
                    "jurisdiction": meta.get("jurisdiction"),
                    "modality": meta.get("modality", "text"),
                    "metadata": json.dumps(meta, default=str),
                    "embedding": to_vector_literal(chunk.embedding),
                    "org_id": org,
                    "effective_date": _coerce_date(meta.get("effective_date")),
                }
            )
        if not rows:
            return 0

        with connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO chunks (
                        chunk_id, doc_id, parent_id, text, chunk_index, tokens,
                        section, level, title, source, service_line, jurisdiction,
                        modality, metadata, embedding, org_id, effective_date
                    )
                    VALUES (
                        :chunk_id, :doc_id, :parent_id, :text, :chunk_index, :tokens,
                        :section, :level, :title, :source, :service_line, :jurisdiction,
                        :modality, CAST(:metadata AS jsonb), CAST(:embedding AS vector),
                        :org_id, :effective_date
                    )
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        doc_id       = EXCLUDED.doc_id,
                        parent_id    = EXCLUDED.parent_id,
                        text         = EXCLUDED.text,
                        chunk_index  = EXCLUDED.chunk_index,
                        tokens       = EXCLUDED.tokens,
                        section      = EXCLUDED.section,
                        level        = EXCLUDED.level,
                        title        = EXCLUDED.title,
                        source       = EXCLUDED.source,
                        service_line = EXCLUDED.service_line,
                        jurisdiction = EXCLUDED.jurisdiction,
                        modality     = EXCLUDED.modality,
                        metadata     = EXCLUDED.metadata,
                        embedding    = EXCLUDED.embedding,
                        org_id       = EXCLUDED.org_id,
                        effective_date = EXCLUDED.effective_date,
                        updated_at   = now()
                    """
                ),
                rows,
            )
        logger.info("upserted_chunks", count=len(rows))
        return len(rows)

    def link_entities(self, chunk_id: str, entities: list[str]) -> None:
        """Port of MERGE (c)-[:MENTIONS]->(e) onto the adjacency table."""
        if not entities:
            return
        names = sorted({e.strip() for e in entities if e and e.strip()})
        if not names:
            return
        with connection() as conn:
            conn.execute(
                text(
                    """
                    WITH input(name) AS (SELECT unnest(CAST(:names AS text[]))),
                    upserted AS (
                        INSERT INTO entities (name)
                        SELECT name FROM input
                        ON CONFLICT (name) DO NOTHING
                        RETURNING entity_id, name
                    ),
                    resolved AS (
                        SELECT entity_id FROM upserted
                        UNION
                        SELECT e.entity_id FROM entities e JOIN input i ON i.name = e.name
                    )
                    INSERT INTO chunk_entities (chunk_id, entity_id)
                    SELECT :chunk_id, entity_id FROM resolved
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"chunk_id": chunk_id, "names": names},
            )

    # -------------------------------------------------------------- retrieval

    def hybrid_search_rrf(
        self,
        embedding: list[float],
        query_text: str,
        top_k: int = 12,
        service_line: str | None = None,
        org_id: str | None = None,
        as_of: date | None = None,
    ) -> list[dict[str, Any]]:
        """Dense + sparse retrieval fused with Reciprocal Rank Fusion.

            RRF(d) = sum over rankers r of  1 / (k + rank_r(d))

        Only *rank position* feeds the fusion, never raw scores -- ts_rank_cd
        output and cosine distance live on incomparable scales, and min-max
        normalising them destroys absolute relevance (it pins the best candidate
        to 1.0 no matter how poor it is).

        Raw cosine is returned alongside as a separate, absolute signal so callers
        can still make an abstention decision.
        """
        settings = self.settings
        scope = _scope_predicates(as_of)
        params = {
            "qvec": to_vector_literal(embedding),
            "qtext": query_text,
            "service_line": service_line,
            "candidates": settings.rrf_candidates,
            "k": settings.rrf_k,
            "top_k": top_k,
            "org_id": org_id or settings.default_org_id,
        }
        if as_of is not None:
            params["as_of"] = as_of
        scope_c = _scope_predicates(as_of, alias="c")
        sql = f"""
        WITH qry AS (SELECT {_OR_TSQUERY} AS tq),
        dense AS (
            SELECT chunk_id,
                   ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:qvec AS vector)) AS rank
            FROM chunks
            WHERE level = 'child'
              AND embedding IS NOT NULL
              AND {scope}
              AND (CAST(:service_line AS text) IS NULL OR service_line = CAST(:service_line AS text))
            ORDER BY embedding <=> CAST(:qvec AS vector)
            LIMIT :candidates
        ),
        sparse AS (
            SELECT c.chunk_id,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank_cd(c.tsv, qry.tq, {_RANK_NORMALIZATION}) DESC
                   ) AS rank
            FROM chunks c CROSS JOIN qry
            WHERE c.level = 'child'
              AND c.tsv @@ qry.tq
              AND {scope_c}
              AND (CAST(:service_line AS text) IS NULL
                   OR c.service_line = CAST(:service_line AS text))
            ORDER BY ts_rank_cd(c.tsv, qry.tq, {_RANK_NORMALIZATION}) DESC
            LIMIT :candidates
        ),
        fused AS (
            SELECT COALESCE(d.chunk_id, s.chunk_id) AS chunk_id,
                   COALESCE(1.0 / (:k + d.rank), 0.0) AS dense_rrf,
                   COALESCE(1.0 / (:k + s.rank), 0.0) AS sparse_rrf,
                   d.rank AS dense_rank,
                   s.rank AS sparse_rank
            FROM dense d
            FULL OUTER JOIN sparse s ON s.chunk_id = d.chunk_id
        )
        SELECT {_CHUNK_COLUMNS},
               (f.dense_rrf + f.sparse_rrf)                AS rrf_score,
               f.dense_rrf                                 AS dense_rrf,
               f.sparse_rrf                                AS sparse_rrf,
               f.dense_rank                                AS dense_rank,
               f.sparse_rank                               AS sparse_rank,
               1 - (c.embedding <=> CAST(:qvec AS vector)) AS cosine
        FROM fused f
        JOIN chunks c ON c.chunk_id = f.chunk_id
        ORDER BY rrf_score DESC
        LIMIT :top_k
        """
        with connection() as conn:
            result = conn.execute(text(sql), params)
            return [dict(row) for row in result.mappings()]

    def vector_search(
        self, embedding: list[float], top_k: int = 12, service_line: str | None = None
    ) -> list[dict[str, Any]]:
        """Dense-only retrieval. Kept for diagnostics and offline evaluation."""
        sql = f"""
        SELECT {_CHUNK_COLUMNS},
               1 - (c.embedding <=> CAST(:qvec AS vector)) AS cosine,
               1 - (c.embedding <=> CAST(:qvec AS vector)) AS score
        FROM chunks c
        WHERE c.level = 'child'
          AND c.embedding IS NOT NULL
          AND (CAST(:service_line AS text) IS NULL OR c.service_line = CAST(:service_line AS text))
        ORDER BY c.embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
        """
        with connection() as conn:
            result = conn.execute(
                text(sql),
                {
                    "qvec": to_vector_literal(embedding),
                    "service_line": service_line,
                    "top_k": top_k,
                },
            )
            return [dict(row) for row in result.mappings()]

    def fulltext_search(self, query_text: str, top_k: int = 12) -> list[dict[str, Any]]:
        """Sparse-only retrieval. Kept for diagnostics and offline evaluation."""
        sql = f"""
        WITH qry AS (SELECT {_OR_TSQUERY} AS tq)
        SELECT {_CHUNK_COLUMNS},
               ts_rank_cd(c.tsv, qry.tq, {_RANK_NORMALIZATION}) AS score
        FROM chunks c CROSS JOIN qry
        WHERE c.level = 'child'
          AND c.tsv @@ qry.tq
        ORDER BY score DESC
        LIMIT :top_k
        """
        with connection() as conn:
            result = conn.execute(text(sql), {"qtext": query_text, "top_k": top_k})
            return [dict(row) for row in result.mappings()]

    def graph_expand(self, chunk_ids: list[str], limit: int = 8) -> list[dict[str, Any]]:
        """Multi-hop expansion over shared entities.

        Equivalent to the Neo4j pattern
        ``(c)-[:MENTIONS]->(e)<-[:MENTIONS]-(related)``, expressed as a self-join
        on the adjacency table. ``score`` is the number of distinct entities each
        neighbour shares with the seed set.
        """
        if not chunk_ids:
            return []
        sql = f"""
        SELECT {_CHUNK_COLUMNS},
               COUNT(DISTINCT seed.entity_id) AS score
        FROM chunk_entities seed
        JOIN chunk_entities rel ON rel.entity_id = seed.entity_id
        JOIN chunks c ON c.chunk_id = rel.chunk_id
        WHERE seed.chunk_id = ANY(CAST(:chunk_ids AS text[]))
          AND rel.chunk_id <> ALL(CAST(:chunk_ids AS text[]))
          AND c.level = 'child'
        GROUP BY {_CHUNK_COLUMNS}
        ORDER BY score DESC
        LIMIT :limit
        """
        with connection() as conn:
            result = conn.execute(
                text(sql), {"chunk_ids": list(chunk_ids), "limit": limit}
            )
            return [dict(row) for row in result.mappings()]

    def fetch_parent_context(self, parent_ids: list[str]) -> list[dict[str, Any]]:
        if not parent_ids:
            return []
        sql = f"""
        SELECT {_CHUNK_COLUMNS}, 1.0 AS score
        FROM chunks c
        WHERE c.chunk_id = ANY(CAST(:parent_ids AS text[]))
        """
        with connection() as conn:
            result = conn.execute(text(sql), {"parent_ids": list(parent_ids)})
            return [dict(row) for row in result.mappings()]

    # ------------------------------------------------------------------ misc

    def stats(self) -> dict[str, int]:
        with connection() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT (SELECT count(*) FROM documents)      AS documents,
                               (SELECT count(*) FROM chunks)         AS chunks,
                               (SELECT count(*) FROM entities)       AS entities,
                               (SELECT count(*) FROM chunk_entities) AS entity_links
                        """
                    )
                )
                .mappings()
                .one()
            )
            return dict(row)

    def close(self) -> None:
        """No-op: connections are returned to the shared pool automatically."""
