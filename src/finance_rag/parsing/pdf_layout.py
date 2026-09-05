"""Layout-aware PDF parsing.

``pypdf.extract_text`` returns a flat character stream: no headings, no table
structure, no reading order. Two consequences drove this module.

First, structure. ``split_by_structure`` keys on markdown headings, and a flat
stream has none, so every PDF collapsed into a single section -- one parent
chunk truncated to 4000 characters for a 113-page publication, with ``section``
null on every child. Hierarchical retrieval was effectively off for exactly the
documents that needed it most.

Second, tables. A depreciation grid flattened to a character stream loses the
row/column association entirely: the percentages survive but nothing connects
20.15 to (Year 3, 5-year property), so the number is present and unusable.

This parser recovers headings from font geometry and tables as discrete blocks,
then renders markdown that the existing structure-aware chunker can consume.
pdfplumber is used rather than PyMuPDF because PyMuPDF is AGPL-3.0, which is a
licensing problem for a commercial deployment; pdfplumber is MIT and needs no ML
model, keeping the container small enough for Fargate cold starts.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from finance_rag.logging_setup import get_logger

logger = get_logger(__name__)

BlockKind = Literal["heading", "paragraph", "table"]

# Ruled-line detection. The text strategy is deliberately not used as a
# fallback: on multi-column pages it slices at character-level column
# boundaries and shreds words mid-token ("he typ e and r e above"), which is
# worse than leaving the region as prose.
_TABLE_SETTINGS = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}

# Collapses newlines too: a wrapped table cell must render on one markdown row,
# and a line-broken heading must not become two blocks.
_WS = re.compile(r"\s+")


@dataclass
class Block:
    kind: BlockKind
    text: str
    page: int
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _body_font_size(pages, sample: int = 30) -> float:
    """Modal font size across a sample of pages: the body text size."""
    from collections import Counter

    counts: Counter[float] = Counter()
    for page in pages[:sample]:
        for word in page.extract_words(extra_attrs=["size"]):
            counts[round(float(word["size"]), 1)] += 1
    return counts.most_common(1)[0][0] if counts else 10.0


def _heading_level(size: float, body: float, bold: bool) -> int:
    """Map font geometry to a heading level; 0 means body text.

    Size alone is not enough: running heads are often larger than body text but
    are furniture, not structure. Requiring bold for the smaller tiers keeps
    page headers out of the document outline.
    """
    ratio = size / body if body else 1.0
    if ratio >= 1.6:
        return 1
    if ratio >= 1.3:
        return 2 if bold else 0
    if ratio >= 1.15 and bold:
        return 3
    return 0


def _table_is_usable(rows: list[list[str]]) -> bool:
    """Reject degenerate extractions.

    A mangled table is worse than no table in an advisory product: it looks like
    structured data and asserts relationships that were never in the document.
    """
    if len(rows) < 2:
        return False
    width = max((len(r) for r in rows), default=0)
    if width < 2:
        return False
    cells = [c for row in rows for c in row]
    filled = sum(1 for c in cells if (c or "").strip())
    return bool(cells) and filled / len(cells) >= 0.3


def _cell(value: str | None) -> str:
    # A literal pipe inside a cell would terminate the column early and shift
    # every value after it into the wrong column.
    return _clean(value or "").replace("|", "\\|")


def _rows_to_markdown(rows: list[list[str]]) -> str:
    width = max(len(r) for r in rows)
    norm = [[_cell(c) for c in r] + [""] * (width - len(r)) for r in rows]
    header, body = norm[0], norm[1:]
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


def _words_outside(words, boxes) -> list[dict]:
    """Drop words falling inside an already-extracted table region.

    Without this the table content appears twice: once as a structured block and
    again as loose prose, doubling its weight in both rankers.
    """
    if not boxes:
        return list(words)
    kept = []
    for w in words:
        cx = (float(w["x0"]) + float(w["x1"])) / 2
        cy = (float(w["top"]) + float(w["bottom"])) / 2
        if not any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes):
            kept.append(w)
    return kept


def _page_blocks(page, body_size: float, page_no: int) -> list[Block]:
    blocks: list[Block] = []
    boxes: list[tuple[float, float, float, float]] = []

    try:
        for table in page.find_tables(_TABLE_SETTINGS):
            rows = table.extract()
            if not _table_is_usable(rows):
                # Leave the region to the prose path rather than emit a table
                # asserting relationships the extraction did not establish.
                continue
            boxes.append(tuple(table.bbox))
            blocks.append(
                Block(kind="table", text=_rows_to_markdown(rows), page=page_no, rows=rows)
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("table_extraction_failed", page=page_no, error=str(exc))

    try:
        words = page.extract_words(extra_attrs=["size", "fontname"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("word_extraction_failed", page=page_no, error=str(exc))
        return blocks

    lines: dict[int, list[dict]] = {}
    for word in _words_outside(words, boxes):
        lines.setdefault(round(float(word["top"])), []).append(word)

    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            text = _clean(" ".join(buffer))
            if text:
                blocks.append(Block(kind="paragraph", text=text, page=page_no))
            buffer.clear()

    for top in sorted(lines):
        group = sorted(lines[top], key=lambda w: float(w["x0"]))
        text = _clean(" ".join(w["text"] for w in group))
        if not text:
            continue
        size = max(float(w["size"]) for w in group)
        bold = any("Bold" in (w.get("fontname") or "") for w in group)
        level = _heading_level(size, body_size, bold)
        if level:
            flush()
            blocks.append(Block(kind="heading", text=text, page=page_no, level=level))
        else:
            buffer.append(text)
    flush()
    return blocks


def parse_pdf(path: Path | str, max_pages: int | None = None) -> list[Block]:
    """Parse a PDF into typed blocks in reading order."""
    import pdfplumber

    blocks: list[Block] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pdfplumber.open(str(path)) as pdf:
            pages = pdf.pages[:max_pages] if max_pages else pdf.pages
            body_size = _body_font_size(pages)
            for i, page in enumerate(pages, start=1):
                blocks.extend(_page_blocks(page, body_size, i))

    logger.info(
        "pdf_layout_parsed",
        path=str(path),
        blocks=len(blocks),
        headings=sum(1 for b in blocks if b.kind == "heading"),
        tables=sum(1 for b in blocks if b.kind == "table"),
    )
    return blocks


def blocks_to_markdown(blocks: list[Block]) -> str:
    """Render blocks as markdown so the structure-aware chunker can split it.

    Headings become real ``#`` markers, which is what gives PDFs a section
    hierarchy -- and therefore working parent/child chunks -- for the first time.
    """
    parts: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            parts.append(f"{'#' * min(block.level, 6)} {block.text}")
        elif block.kind == "table":
            parts.append(block.text)
        else:
            parts.append(block.text)
    return "\n\n".join(p for p in parts if p.strip())
