"""Document ingestion for Source Advisors knowledge corpus."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

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


_DIGIT_RUN = re.compile(r"\d+")
_DOT_LEADER = re.compile(r"(?:\s*\.){4,}")
_WS_RUN = re.compile(r"[ \t]{2,}")


def strip_page_furniture(
    pages: list[str], min_pages: int = 3, repeat_fraction: float = 0.5, edge_lines: int = 3
) -> str:
    """Drop headers and footers that repeat across a document's pages.

    Page furniture is otherwise indexed as though it were content. In IRS
    Publication 946 the line "The type and rule above prints on all proofs..."
    appears on all 113 pages and the "Page N of M Fileid:..." footer on 112.
    Embedded and tokenised that many times it dilutes every vector and hands the
    lexical ranker a large supply of keyword-dense passages carrying no
    information at all.

    Detection is structural rather than a list of known strings, so it
    generalises to any publisher: digit runs are normalised before counting, so
    "Page 39 of 113" and "Page 40 of 113" collapse to a single recurring line.

    Two constraints keep that normalisation from eating real content. Only lines
    within ``edge_lines`` of the top or bottom of a page are candidates, because
    that is where headers and footers live and body text does not; and a line
    must recur on ``repeat_fraction`` of pages. Without the positional
    constraint, tabular rows differing only by digits -- "Year 3 ... 20.00%" --
    normalise to one key and would be deleted as furniture.
    """
    if len(pages) < min_pages:
        return "\n\n".join(pages)

    def _key(line: str) -> str:
        return _DIGIT_RUN.sub("#", line.strip())

    def _edges(lines: list[str]) -> list[str]:
        if len(lines) <= edge_lines * 2:
            return lines
        return lines[:edge_lines] + lines[-edge_lines:]

    counts: Counter[str] = Counter()
    for page in pages:
        # Count each candidate once per page: a phrase repeated within a single
        # page is content, a phrase appearing on every page is furniture.
        candidates = _edges(page.splitlines())
        counts.update({_key(ln) for ln in candidates if len(ln.strip()) > 15})

    threshold = max(2, int(len(pages) * repeat_fraction))
    furniture = {key for key, count in counts.items() if count >= threshold}

    cleaned = []
    for page in pages:
        lines = page.splitlines()
        edge_keys = {_key(ln) for ln in _edges(lines)}
        cleaned.append(
            "\n".join(
                ln
                for ln in lines
                # Only strip at the edges: an identical string appearing mid-page
                # is body text that happens to match a header.
                if not (_key(ln) in furniture and _key(ln) in edge_keys)
            )
        )

    text = "\n\n".join(cleaned)
    # Fillable-form dot leaders ("42 . . . . . . 38") carry no meaning and
    # survive tokenisation as noise.
    text = _DOT_LEADER.sub(" ", text)
    text = _WS_RUN.sub(" ", text)

    if furniture:
        logger.info("stripped_page_furniture", lines=len(furniture), pages=len(pages))
    return text


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


def _load_pdf_text(path: Path) -> str:
    """Extract PDF text, preferring layout-aware parsing.

    The layout parser recovers headings and tables, which is what gives a PDF a
    section hierarchy at all -- without it ``split_by_structure`` finds no
    markdown headings and collapses the whole document into one section.

    Falls back to the flat pypdf stream if parsing fails or yields no structure,
    so a PDF the layout parser cannot handle still gets indexed rather than
    silently dropped.
    """
    try:
        from finance_rag.parsing import blocks_to_markdown, parse_pdf

        blocks = parse_pdf(path)
        headings = sum(1 for b in blocks if b.kind == "heading")
        if headings:
            return blocks_to_markdown(blocks)
        logger.info("layout_parse_without_structure", path=str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("layout_parse_failed", path=str(path), error=str(exc))

    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return strip_page_furniture([page.extract_text() or "" for page in reader.pages])


def load_pdf(path: Path) -> tuple[str, DocumentMeta]:
    text = _load_pdf_text(path)
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


def ingest_paths(
    paths: Iterable[Path], _seen: set[Path] | None = None
) -> list[tuple[str, DocumentMeta]]:
    """Ingest files, expanding directories recursively.

    ``_seen`` de-duplicates by resolved path. Without it, a directory argument
    yields every nested file twice: ``rglob("*")`` returns sub-directories as
    well as their contents, so recursing into those sub-directories re-visits
    files the parent glob already produced. Duplicates are invisible in the
    store (chunk ids are content-derived, so they upsert over themselves) but
    every duplicate is embedded again at full cost, and document counts are
    reported inflated.
    """
    documents: list[tuple[str, DocumentMeta]] = []
    seen = _seen if _seen is not None else set()
    for path in paths:
        path = Path(path)
        if not path.exists():
            logger.warning("skip_missing_path", path=str(path))
            continue
        if path.is_dir():
            # Pass files only; sub-directories are already covered by rglob.
            nested = sorted(p for p in path.rglob("*") if p.is_file())
            documents.extend(ingest_paths(nested, _seen=seen))
            continue

        resolved = path.resolve()
        if resolved in seen:
            logger.debug("skip_duplicate_path", path=str(path))
            continue
        seen.add(resolved)

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
        except Exception as exc:
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
