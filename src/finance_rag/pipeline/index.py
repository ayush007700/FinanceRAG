"""End-to-end corpus indexing pipeline."""

from __future__ import annotations

from pathlib import Path

from finance_rag.chunking import chunk_corpus
from finance_rag.embeddings import EmbeddingService
from finance_rag.graph import Neo4jKnowledgeStore, extract_entities
from finance_rag.ingestion import ingest_paths
from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)


def index_corpus(paths: list[str | Path], link_entities: bool = True) -> dict:
    documents = ingest_paths([Path(p) for p in paths])
    if not documents:
        raise ValueError("No documents ingested")

    chunks = chunk_corpus(documents)
    embedder = EmbeddingService()
    chunks = embedder.embed_chunks(chunks)

    store = Neo4jKnowledgeStore()
    store.ensure_schema()
    for _, meta in documents:
        store.upsert_document(meta)
    store.upsert_chunks(chunks)

    entity_links = 0
    if link_entities:
        for chunk in chunks:
            if chunk.metadata.get("level") != "child":
                continue
            entities = extract_entities(chunk.text)
            if entities:
                store.link_entities(chunk.chunk_id, entities)
                entity_links += len(entities)

    logger.info(
        "index_complete",
        documents=len(documents),
        chunks=len(chunks),
        entity_links=entity_links,
    )
    return {
        "documents": len(documents),
        "chunks": len(chunks),
        "entity_links": entity_links,
    }
