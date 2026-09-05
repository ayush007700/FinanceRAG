"""Extract images from PDFs / image files and turn them into caption chunks."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path

from finance_rag.chunking.hierarchical import count_tokens
from finance_rag.config import get_settings
from finance_rag.logging_setup import get_logger
from finance_rag.models import Chunk, DocumentMeta
from finance_rag.multimodal.vision import VisionCaptioner

logger = get_logger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _save_media(doc_id: str, index: int, image_bytes: bytes, ext: str) -> str:
    settings = get_settings()
    media_root = Path(settings.media_dir) / doc_id
    media_root.mkdir(parents=True, exist_ok=True)
    path = media_root / f"img_{index:03d}{ext}"
    path.write_bytes(image_bytes)
    return str(path)


def _chunk_from_caption(
    *,
    doc_id: str,
    meta: DocumentMeta,
    caption: str,
    image_path: str,
    index: int,
    page: int | None = None,
) -> Chunk:
    text = (
        f"[Image caption] {caption}\n"
        f"(source_image={image_path}"
        + (f", page={page}" if page is not None else "")
        + ")"
    )
    return Chunk(
        chunk_id=f"{doc_id}_{_stable_id(doc_id, str(index), caption[:80])}",
        doc_id=doc_id,
        text=text,
        index=index,
        tokens=count_tokens(text),
        section=f"image:{index}",
        parent_id=None,
        metadata={
            **asdict(meta),
            "level": "child",
            "modality": "image",
            "image_path": image_path,
            "page": page,
            "service_line": meta.service_line,
            "jurisdiction": meta.jurisdiction,
        },
    )


def ingest_image_file(path: Path, captioner: VisionCaptioner | None = None) -> list[Chunk]:
    settings = get_settings()
    if not settings.multimodal_enabled:
        return []
    captioner = captioner or VisionCaptioner()
    data = path.read_bytes()
    doc_id = _stable_id(str(path.resolve()))
    meta = DocumentMeta(
        doc_id=doc_id,
        source=str(path),
        title=path.stem.replace("_", " ").title(),
        doc_type="image",
        tags=["multimodal", "image"],
    )
    saved = _save_media(doc_id, 0, data, path.suffix.lower() or ".png")
    caption = captioner.caption_path(str(path))
    return [_chunk_from_caption(doc_id=doc_id, meta=meta, caption=caption, image_path=saved, index=0)]


def extract_pdf_image_chunks(path: Path, captioner: VisionCaptioner | None = None) -> list[Chunk]:
    settings = get_settings()
    if not settings.multimodal_enabled:
        return []
    from pypdf import PdfReader

    captioner = captioner or VisionCaptioner()
    reader = PdfReader(str(path))
    doc_id = _stable_id(str(path.resolve()))
    meta = DocumentMeta(
        doc_id=doc_id,
        source=str(path),
        title=path.stem.replace("_", " ").title(),
        doc_type="pdf",
        tags=["multimodal", "pdf"],
    )
    chunks: list[Chunk] = []
    img_index = 0
    for page_num, page in enumerate(reader.pages, start=1):
        try:
            images = page.images
        except Exception as exc:  # noqa: BLE001
            # Malformed image XObjects are common in IRS PDFs; skip the page but
            # leave a trace, otherwise a systematically unreadable document looks
            # identical to one that simply has no figures.
            logger.warning("pdf_page_images_unreadable", page=page_num, error=str(exc))
            continue
        for image in images:
            if img_index >= settings.max_images_per_doc:
                break
            try:
                data = image.data
                name = getattr(image, "name", f"p{page_num}_{img_index}.png") or "img.png"
                ext = Path(name).suffix.lower() or ".png"
                if ext not in IMAGE_EXTS:
                    ext = ".png"
                saved = _save_media(doc_id, img_index, data, ext)
                caption = captioner.caption_bytes(
                    data, mime="image/jpeg" if ext in {".jpg", ".jpeg"} else "image/png"
                )
                chunks.append(
                    _chunk_from_caption(
                        doc_id=doc_id,
                        meta=meta,
                        caption=caption,
                        image_path=saved,
                        index=10_000 + img_index,
                        page=page_num,
                    )
                )
                img_index += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("pdf_image_skip", page=page_num, error=str(exc))
        if img_index >= settings.max_images_per_doc:
            break
    logger.info("pdf_images_ingested", path=str(path), count=len(chunks))
    return chunks


def multimodal_chunks_for_path(path: Path, captioner: VisionCaptioner | None = None) -> list[Chunk]:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTS:
        return ingest_image_file(path, captioner=captioner)
    if suffix == ".pdf":
        return extract_pdf_image_chunks(path, captioner=captioner)
    return []
