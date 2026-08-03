"""Document ingestion for Source Advisors knowledge corpus."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

from finance_rag.logging_setup import get_logger
from finance_rag.models import DocumentMeta

logger = get_logger(__name__)

SERVICE_LINE_HINTS = {
    "r&d": "R&D Tax Credit",
    "research credit": "R&D Tax Credit",
    "cost segregation": "Cost Segregation",
    "179d": "Energy Efficiency §179D",
    "45l": "Energy Efficiency §45L",
    "sales and use": "Sales & Use Tax",
    "sales & use": "Sales & Use Tax",
    "investment tax credit": "Investment Tax Credit",
    "production tax credit": "Production Tax Credit",
    "property tax": "Commercial Property Tax",
    "lifo": "LIFO Inventory",
}


def _stable_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def infer_service_line(text: str) -> str | None:
    lowered = text.lower()
    for hint, label in SERVICE_LINE_HINTS.items():
        if hint in lowered:
            return label
    return None


def infer_jurisdiction(text: str) -> str | None:
    lowered = text.lower()
    if "united kingdom" in lowered or " hmrc " in f" {lowered} " or "uk " in lowered[:40]:
        return "UK"
    if "irs" in lowered or "irc" in lowered or "united states" in lowered or "usa" in lowered:
        return "USA"
    return "USA & UK"


def load_text_file(path: Path) -> tuple[str, DocumentMeta]:
    raw = path.read_text(encoding="utf-8")
    title = path.stem.replace("_", " ").title()
    first_heading = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
    if first_heading:
        title = first_heading.group(1).strip()

    meta = DocumentMeta(
        doc_id=_stable_id(str(path.resolve())),
        source=str(path),
        title=title,
        service_line=infer_service_line(raw),
        jurisdiction=infer_jurisdiction(raw),
        doc_type=path.suffix.lstrip(".") or "txt",
        tags=_extract_tags(raw),
    )
    return raw, meta


def load_json_corpus(path: Path) -> list[tuple[str, DocumentMeta]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    docs: list[tuple[str, DocumentMeta]] = []
    for item in payload:
        text = item["text"]
        meta = DocumentMeta(
            doc_id=item.get("doc_id") or _stable_id(text[:500]),
            source=item.get("source", str(path)),
            title=item.get("title", "Untitled"),
            service_line=item.get("service_line") or infer_service_line(text),
            jurisdiction=item.get("jurisdiction") or infer_jurisdiction(text),
            doc_type=item.get("doc_type", "knowledge"),
            effective_date=item.get("effective_date"),
            tags=item.get("tags", []),
            extra={k: v for k, v in item.items() if k not in {"text", "doc_id", "source", "title"}},
        )
        docs.append((text, meta))
    return docs


def load_pdf(path: Path) -> tuple[str, DocumentMeta]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(pages)
    meta = DocumentMeta(
        doc_id=_stable_id(str(path.resolve())),
        source=str(path),
        title=path.stem.replace("_", " ").title(),
        service_line=infer_service_line(text),
        jurisdiction=infer_jurisdiction(text),
        doc_type="pdf",
        tags=_extract_tags(text),
    )
    return text, meta


def ingest_paths(paths: Iterable[Path]) -> list[tuple[str, DocumentMeta]]:
    documents: list[tuple[str, DocumentMeta]] = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            logger.warning("skip_missing_path", path=str(path))
            continue
        if path.is_dir():
            documents.extend(ingest_paths(sorted(path.rglob("*"))))
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in {".md", ".txt"}:
                documents.append(load_text_file(path))
            elif suffix == ".json":
                documents.extend(load_json_corpus(path))
            elif suffix == ".pdf":
                documents.append(load_pdf(path))
            else:
                continue
            logger.info("ingested_document", path=str(path), suffix=suffix)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ingest_failed", path=str(path), error=str(exc))
    return documents


def _extract_tags(text: str) -> list[str]:
    tags = set()
    for match in re.findall(r"§\s?\d+[A-Za-z]?", text):
        tags.add(match.replace(" ", ""))
    for code in ("IRC", "IRS", "HMRC", "ITC", "PTC", "LIFO", "ASC"):
        if re.search(rf"\b{code}\b", text, flags=re.IGNORECASE):
            tags.add(code)
    return sorted(tags)
