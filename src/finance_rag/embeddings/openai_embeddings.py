"""OpenAI embedding client with batching and optional dimensionality control."""

from __future__ import annotations

from typing import Sequence

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.models import Chunk

logger = get_logger(__name__)


class EmbeddingService:
    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key or None)
        self.model = settings.openai_embedding_model
        self.dimensions = settings.openai_embedding_dimensions

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(4))
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # text-embedding-3-* supports dimensions reduction
        kwargs = {
            "model": self.model,
            "input": list(texts),
        }
        if self.model.startswith("text-embedding-3"):
            kwargs["dimensions"] = self.dimensions

        response = self.client.embeddings.create(**kwargs)
        vectors = [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
        logger.info("embedded_batch", count=len(vectors), model=self.model)
        return vectors

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0]

    def embed_chunks(self, chunks: list[Chunk], batch_size: int = 64) -> list[Chunk]:
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = self.embed_texts([c.text for c in batch])
            for chunk, vector in zip(batch, vectors, strict=True):
                chunk.embedding = vector
        return chunks
