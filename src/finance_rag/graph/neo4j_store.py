"""Neo4j graph + vector knowledge store for Source Advisors RAG."""

from __future__ import annotations

import json
from typing import Any

from neo4j import GraphDatabase

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.models import Chunk, DocumentMeta

logger = get_logger(__name__)


SCHEMA_QUERIES = [
    """
    CREATE CONSTRAINT document_id IF NOT EXISTS
    FOR (d:Document) REQUIRE d.doc_id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT chunk_id IF NOT EXISTS
    FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE
    """,
    """
    CREATE CONSTRAINT service_name IF NOT EXISTS
    FOR (s:ServiceLine) REQUIRE s.name IS UNIQUE
    """,
    """
    CREATE CONSTRAINT entity_name IF NOT EXISTS
    FOR (e:Entity) REQUIRE e.name IS UNIQUE
    """,
]


class Neo4jKnowledgeStore:
    def __init__(self) -> None:
        settings = get_settings()
        self.database = settings.neo4j_database
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        self.embedding_dim = settings.openai_embedding_dimensions

    def close(self) -> None:
        self.driver.close()

    def ensure_schema(self) -> None:
        with self.driver.session(database=self.database) as session:
            for query in SCHEMA_QUERIES:
                session.run(query)
            # Vector index for child chunks (Neo4j 5.x)
            session.run(
                """
                CREATE VECTOR INDEX chunk_embedding IF NOT EXISTS
                FOR (c:Chunk)
                ON c.embedding
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: $dims,
                    `vector.similarity_function`: 'cosine'
                  }
                }
                """,
                dims=self.embedding_dim,
            )
            session.run(
                """
                CREATE FULLTEXT INDEX chunk_fulltext IF NOT EXISTS
                FOR (c:Chunk) ON EACH [c.text, c.section, c.title]
                """
            )
        logger.info("neo4j_schema_ready")

    def upsert_document(self, meta: DocumentMeta) -> None:
        with self.driver.session(database=self.database) as session:
            session.run(
                """
                MERGE (d:Document {doc_id: $doc_id})
                SET d.title = $title,
                    d.source = $source,
                    d.doc_type = $doc_type,
                    d.jurisdiction = $jurisdiction,
                    d.effective_date = $effective_date,
                    d.tags = $tags
                WITH d
                FOREACH (_ IN CASE WHEN $service_line IS NULL THEN [] ELSE [1] END |
                  MERGE (s:ServiceLine {name: $service_line})
                  MERGE (d)-[:ABOUT_SERVICE]->(s)
                )
                """,
                doc_id=meta.doc_id,
                title=meta.title,
                source=meta.source,
                doc_type=meta.doc_type,
                jurisdiction=meta.jurisdiction,
                effective_date=meta.effective_date,
                tags=meta.tags,
                service_line=meta.service_line,
            )

    def upsert_chunks(self, chunks: list[Chunk]) -> None:
        rows = []
        for chunk in chunks:
            if not chunk.embedding:
                continue
            rows.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "text": chunk.text,
                    "index": chunk.index,
                    "tokens": chunk.tokens,
                    "section": chunk.section,
                    "parent_id": chunk.parent_id,
                    "level": chunk.metadata.get("level", "child"),
                    "title": chunk.metadata.get("title"),
                    "service_line": chunk.metadata.get("service_line"),
                    "jurisdiction": chunk.metadata.get("jurisdiction"),
                    "source": chunk.metadata.get("source"),
                    "embedding": chunk.embedding,
                    "metadata_json": json.dumps(chunk.metadata),
                }
            )

        with self.driver.session(database=self.database) as session:
            session.run(
                """
                UNWIND $rows AS row
                MERGE (c:Chunk {chunk_id: row.chunk_id})
                SET c.text = row.text,
                    c.index = row.index,
                    c.tokens = row.tokens,
                    c.section = row.section,
                    c.level = row.level,
                    c.title = row.title,
                    c.service_line = row.service_line,
                    c.jurisdiction = row.jurisdiction,
                    c.source = row.source,
                    c.metadata_json = row.metadata_json,
                    c.embedding = row.embedding
                WITH c, row
                MATCH (d:Document {doc_id: row.doc_id})
                MERGE (d)-[:HAS_CHUNK]->(c)
                WITH c, row
                FOREACH (_ IN CASE WHEN row.parent_id IS NULL THEN [] ELSE [1] END |
                  MERGE (p:Chunk {chunk_id: row.parent_id})
                  MERGE (c)-[:CHILD_OF]->(p)
                )
                WITH c, row
                FOREACH (_ IN CASE WHEN row.service_line IS NULL THEN [] ELSE [1] END |
                  MERGE (s:ServiceLine {name: row.service_line})
                  MERGE (c)-[:MENTIONS_SERVICE]->(s)
                )
                """,
                rows=rows,
            )
        logger.info("upserted_chunks", count=len(rows))

    def link_entities(self, chunk_id: str, entities: list[str]) -> None:
        with self.driver.session(database=self.database) as session:
            session.run(
                """
                MATCH (c:Chunk {chunk_id: $chunk_id})
                UNWIND $entities AS name
                MERGE (e:Entity {name: name})
                MERGE (c)-[:MENTIONS]->(e)
                """,
                chunk_id=chunk_id,
                entities=entities,
            )

    def vector_search(
        self, embedding: list[float], top_k: int = 12, service_line: str | None = None
    ) -> list[dict[str, Any]]:
        filters = "WHERE c.level = 'child'"
        if service_line:
            filters += " AND c.service_line = $service_line"
        query = f"""
        CALL db.index.vector.queryNodes('chunk_embedding', $top_k, $embedding)
        YIELD node, score
        WITH node AS c, score
        {filters}
        RETURN c.chunk_id AS chunk_id,
               c.doc_id AS doc_id,
               c.text AS text,
               c.section AS section,
               c.title AS title,
               c.source AS source,
               c.service_line AS service_line,
               c.jurisdiction AS jurisdiction,
               c.parent_id AS parent_id,
               c.metadata_json AS metadata_json,
               score
        ORDER BY score DESC
        LIMIT $top_k
        """
        with self.driver.session(database=self.database) as session:
            result = session.run(
                query, embedding=embedding, top_k=top_k * 2, service_line=service_line
            )
            return [dict(record) for record in result][:top_k]

    def fulltext_search(self, query_text: str, top_k: int = 12) -> list[dict[str, Any]]:
        with self.driver.session(database=self.database) as session:
            result = session.run(
                """
                CALL db.index.fulltext.queryNodes('chunk_fulltext', $q)
                YIELD node, score
                WHERE node.level = 'child'
                RETURN node.chunk_id AS chunk_id,
                       node.doc_id AS doc_id,
                       node.text AS text,
                       node.section AS section,
                       node.title AS title,
                       node.source AS source,
                       node.service_line AS service_line,
                       node.jurisdiction AS jurisdiction,
                       node.parent_id AS parent_id,
                       node.metadata_json AS metadata_json,
                       score
                ORDER BY score DESC
                LIMIT $top_k
                """,
                q=query_text,
                top_k=top_k,
            )
            return [dict(record) for record in result]

    def graph_expand(self, chunk_ids: list[str], limit: int = 8) -> list[dict[str, Any]]:
        """Expand via shared entities / service lines for multi-hop context."""
        with self.driver.session(database=self.database) as session:
            result = session.run(
                """
                MATCH (c:Chunk)
                WHERE c.chunk_id IN $chunk_ids
                OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(related:Chunk)
                WHERE related.chunk_id <> c.chunk_id AND related.level = 'child'
                WITH related, count(*) AS shared
                WHERE related IS NOT NULL
                RETURN related.chunk_id AS chunk_id,
                       related.doc_id AS doc_id,
                       related.text AS text,
                       related.section AS section,
                       related.title AS title,
                       related.source AS source,
                       related.service_line AS service_line,
                       related.jurisdiction AS jurisdiction,
                       related.parent_id AS parent_id,
                       related.metadata_json AS metadata_json,
                       shared AS score
                ORDER BY shared DESC
                LIMIT $limit
                """,
                chunk_ids=chunk_ids,
                limit=limit,
            )
            return [dict(record) for record in result]

    def fetch_parent_context(self, parent_ids: list[str]) -> list[dict[str, Any]]:
        with self.driver.session(database=self.database) as session:
            result = session.run(
                """
                MATCH (p:Chunk)
                WHERE p.chunk_id IN $parent_ids
                RETURN p.chunk_id AS chunk_id,
                       p.doc_id AS doc_id,
                       p.text AS text,
                       p.section AS section,
                       p.title AS title,
                       p.source AS source,
                       p.service_line AS service_line,
                       p.jurisdiction AS jurisdiction,
                       p.parent_id AS parent_id,
                       p.metadata_json AS metadata_json,
                       1.0 AS score
                """,
                parent_ids=parent_ids,
            )
            return [dict(record) for record in result]
