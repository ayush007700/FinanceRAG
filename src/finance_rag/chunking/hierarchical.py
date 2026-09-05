"""Hierarchical + structure-aware chunking for tax/finance documents."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict

import tiktoken

from finance_rag.config import get_settings
from finance_rag.models import Chunk, DocumentMeta

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
SECTION_SPLIT_RE = re.compile(r"\n(?=#{1,6}\s)")


def _encoder():
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoder().encode(text))


def _chunk_id(doc_id: str, index: int, text: str) -> str:
    digest = hashlib.sha256(f"{doc_id}:{index}:{text[:120]}".encode()).hexdigest()[:16]
    return f"{doc_id}_{digest}"


def split_by_structure(text: str) -> list[tuple[str | None, str]]:
    """Split markdown/plain tax docs into (section_title, body) pairs."""
    parts = SECTION_SPLIT_RE.split(text.strip())
    sections: list[tuple[str | None, str]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        heading = HEADING_RE.match(part)
        if heading:
            title = heading.group(2).strip()
            body = part[heading.end() :].strip()
            sections.append((title, body or part))
        else:
            sections.append((None, part))
    return sections or [(None, text.strip())]


_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


def split_off_tables(text: str) -> list[tuple[bool, str]]:
    """Split text into ``(is_table, segment)`` runs.

    Markdown tables must survive chunking intact. Splitting one mid-way strands
    the rows in a chunk with no header, which is worse than dropping the table:
    the numbers stay readable while the column they belong to disappears, so a
    depreciation rate can be attributed to the wrong recovery period.
    """
    segments: list[tuple[bool, str]] = []
    buffer: list[str] = []
    in_table = False

    def flush() -> None:
        if buffer:
            body = "\n".join(buffer).strip()
            if body:
                segments.append((in_table, body))
            buffer.clear()

    for line in text.splitlines():
        is_table_line = bool(_TABLE_LINE.match(line))
        if is_table_line != in_table:
            flush()
            in_table = is_table_line
        buffer.append(line)
    flush()
    return segments


def recursive_token_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    enc = _encoder()
    tokens = enc.encode(text)
    if len(tokens) <= chunk_size:
        return [text] if text.strip() else []

    separators = ["\n\n", "\n", ". ", "; ", ", ", " "]
    pieces: list[str] = [text]
    for sep in separators:
        next_pieces: list[str] = []
        for piece in pieces:
            if count_tokens(piece) <= chunk_size:
                next_pieces.append(piece)
            else:
                next_pieces.extend([p for p in piece.split(sep) if p.strip()])
        pieces = next_pieces

    # Pack token windows with overlap
    packed: list[str] = []
    buffer: list[int] = []
    for piece in pieces:
        piece_tokens = enc.encode(piece if piece.endswith((" ", "\n")) else piece + " ")
        if len(buffer) + len(piece_tokens) > chunk_size and buffer:
            packed.append(enc.decode(buffer).strip())
            if overlap > 0:
                buffer = buffer[-overlap:]
            else:
                buffer = []
        buffer.extend(piece_tokens)
    if buffer:
        packed.append(enc.decode(buffer).strip())
    return [p for p in packed if p]


def chunk_document(text: str, meta: DocumentMeta) -> list[Chunk]:
    settings = get_settings()
    chunks: list[Chunk] = []
    index = 0

    for section_title, body in split_by_structure(text):
        parent_text = body
        parent_id = None
        parent_tokens = count_tokens(parent_text)

        # Parent (section-level) chunk for hierarchical retrieval
        if parent_tokens > 0:
            parent_id = _chunk_id(meta.doc_id, index, parent_text[:200])
            parent_chunk = Chunk(
                chunk_id=parent_id,
                doc_id=meta.doc_id,
                text=parent_text[:4000],
                index=index,
                tokens=min(parent_tokens, count_tokens(parent_text[:4000])),
                section=section_title,
                parent_id=None,
                metadata={
                    **asdict(meta),
                    "level": "parent",
                    "service_line": meta.service_line,
                    "jurisdiction": meta.jurisdiction,
                },
            )
            chunks.append(parent_chunk)
            index += 1

        # Always emit child chunks for vector/fulltext retrieval (parents are context-only).
        for is_table, segment in split_off_tables(body):
            if is_table:
                # Emitted whole even when oversized: a partial table is
                # actively misleading, whereas a long one is merely expensive.
                child_texts: list[tuple[str, str]] = [(segment, "table")]
            else:
                child_texts = [
                    (piece, "text")
                    for piece in recursive_token_split(
                        segment,
                        chunk_size=settings.chunk_size,
                        overlap=settings.chunk_overlap,
                    )
                ]

            for child, modality in child_texts:
                child_chunk = Chunk(
                    chunk_id=_chunk_id(meta.doc_id, index, child),
                    doc_id=meta.doc_id,
                    text=child,
                    index=index,
                    tokens=count_tokens(child),
                    section=section_title,
                    parent_id=parent_id,
                    metadata={
                        **asdict(meta),
                        "level": "child",
                        "modality": modality,
                        "service_line": meta.service_line,
                        "jurisdiction": meta.jurisdiction,
                    },
                )
                chunks.append(child_chunk)
                index += 1

    return chunks


def chunk_corpus(documents: list[tuple[str, DocumentMeta]]) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    for text, meta in documents:
        all_chunks.extend(chunk_document(text, meta))
    return all_chunks
