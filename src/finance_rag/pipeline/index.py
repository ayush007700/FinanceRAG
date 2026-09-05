"""End-to-end corpus indexing pipeline (text + multimodal captions)."""

from __future__ import annotations

from pathlib import Path

from finance_rag.cache import SemanticCache
from finance_rag.chunking import chunk_corpus
from finance_rag.embeddings import EmbeddingService
from finance_rag.ingestion import ingest_paths
from finance_rag.logging_setup import get_logger
from finance_rag.models import Chunk, DocumentMeta
from finance_rag.multimodal import multimodal_chunks_for_path
from finance_rag.store import PgVectorStore, extract_entities

logger = get_logger(__name__)


def index_corpus(
    paths: list[str | Path],
    link_entities: bool = True,
    org_id: str | None = None,
) -> dict:
    path_objs = [Path(p) for p in paths]
    documents = ingest_paths(path_objs)
    chunks = chunk_corpus(documents) if documents else []

    # Multimodal: caption images / PDF-embedded images
    image_chunks: list[Chunk] = []
    image_docs: list[tuple[str, DocumentMeta]] = []
    for path in path_objs:
        targets = sorted(path.rglob("*")) if path.is_dir() else [path]
        for target in targets:
            if not target.is_file():
                continue
            try:
                m_chunks = multimodal_chunks_for_path(target)
            except Exception as exc:  # noqa: BLE001
                logger.warning("multimodal_skip", path=str(target), error=str(exc))
                continue
            if not m_chunks:
                continue
            image_chunks.extend(m_chunks)
            # Register document nodes for image-only sources
            meta = DocumentMeta(
                doc_id=m_chunks[0].doc_id,
                source=str(target),
                title=target.stem.replace("_", " ").title(),
                doc_type=target.suffix.lstrip(".") or "image",
                tags=["multimodal"],
            )
            image_docs.append(("", meta))

    if not chunks and not image_chunks:
        raise ValueError("No documents or images ingested")

    all_chunks = chunks + image_chunks
    embedder = EmbeddingService()
    all_chunks = embedder.embed_chunks(all_chunks)

    store = PgVectorStore()
    store.ensure_schema()
    for _, meta in documents:
        store.upsert_document(meta, org_id=org_id)
    for _, meta in image_docs:
        store.upsert_document(meta, org_id=org_id)
    store.upsert_chunks(all_chunks, org_id=org_id)

    entity_links = 0
    if link_entities:
        for chunk in all_chunks:
            if chunk.metadata.get("level") != "child":
                continue
            entities = extract_entities(chunk.text)
            if entities:
                store.link_entities(chunk.chunk_id, entities)
                entity_links += len(entities)

    # Invalidate/semantic-reset answer cache when corpus changes
    cache_version = SemanticCache(embedder=embedder).bump_corpus_version()

    logger.info(
        "index_complete",
        documents=len(documents) + len(image_docs),
        chunks=len(all_chunks),
        image_chunks=len(image_chunks),
        entity_links=entity_links,
        cache_version=cache_version,
    )
    return {
        "documents": len(documents) + len(image_docs),
        "chunks": len(all_chunks),
        "image_chunks": len(image_chunks),
        "entity_links": entity_links,
        "cache_version": cache_version,
    }
