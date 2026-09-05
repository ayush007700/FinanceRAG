"""Layout-aware PDF parsing and table-safe chunking.

The behaviour under test is what gives a PDF any structure at all. Before this,
``split_by_structure`` found no markdown headings in a flat pypdf stream, so a
113-page publication became one section with a single 4000-character parent and
``section`` null on every child.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finance_rag.parsing.pdf_layout import (
    Block,
    _cell,
    _heading_level,
    _rows_to_markdown,
    _table_is_usable,
    _words_outside,
    blocks_to_markdown,
)

CORPUS = Path("data/corpus/multimodal")
SMALL_PDF = CORPUS / "irs_form_6765_rd_credit.pdf"
INSTRUCTIONS_PDF = CORPUS / "irs_instructions_6765.pdf"

requires_corpus = pytest.mark.skipif(
    not SMALL_PDF.exists(), reason="sample corpus PDFs not present"
)


# --------------------------------------------------------------------------
# heading detection
# --------------------------------------------------------------------------


def test_body_text_is_not_a_heading():
    assert _heading_level(10.0, 10.0, bold=False) == 0
    assert _heading_level(10.0, 10.0, bold=True) == 0


def test_large_text_is_a_top_level_heading():
    assert _heading_level(17.0, 10.0, bold=True) == 1
    assert _heading_level(16.0, 10.0, bold=False) == 1


def test_mid_size_requires_bold():
    """Running heads are often larger than body text but are furniture.

    Requiring bold at the smaller tiers keeps page headers out of the outline.
    """
    assert _heading_level(14.0, 10.0, bold=True) == 2
    assert _heading_level(14.0, 10.0, bold=False) == 0


def test_slightly_larger_bold_is_a_subheading():
    assert _heading_level(11.6, 10.0, bold=True) == 3
    assert _heading_level(11.6, 10.0, bold=False) == 0


# --------------------------------------------------------------------------
# table quality gate
# --------------------------------------------------------------------------


def test_usable_table_accepted():
    assert _table_is_usable([["Part", "Purpose"], ["I", "Electing the deduction"]])


def test_single_column_rejected():
    assert not _table_is_usable([["a"], ["b"], ["c"]])


def test_single_row_rejected():
    assert not _table_is_usable([["Part", "Purpose"]])


def test_mostly_empty_table_rejected():
    """A mangled table asserts relationships the document never stated.

    In an advisory product that is worse than no table: it looks like structured
    data, so a rate can be read against the wrong recovery period.
    """
    sparse = [["", "", ""], ["", "x", ""], ["", "", ""]]
    assert not _table_is_usable(sparse)


# --------------------------------------------------------------------------
# markdown rendering
# --------------------------------------------------------------------------


def test_rows_render_as_markdown():
    md = _rows_to_markdown([["Year", "Rate"], ["1", "20.00%"]])
    assert md.splitlines()[0] == "| Year | Rate |"
    assert md.splitlines()[1] == "|---|---|"
    assert "| 1 | 20.00% |" in md


def test_wrapped_cell_collapses_to_one_row():
    """A cell containing a newline would otherwise break the table apart."""
    md = _rows_to_markdown([["Part", "Purpose"], ["I", "Electing\nthe deduction"]])
    assert len(md.splitlines()) == 3
    assert "Electing the deduction" in md


def test_pipe_in_cell_is_escaped():
    """An unescaped pipe terminates the column early and shifts every value after it."""
    assert _cell("a | b") == "a \\| b"


def test_ragged_rows_are_padded():
    md = _rows_to_markdown([["a", "b", "c"], ["1"]])
    assert md.splitlines()[-1].count("|") == 4


def test_blocks_render_headings_as_markdown():
    blocks = [
        Block(kind="heading", text="Purpose of Form", page=1, level=2),
        Block(kind="paragraph", text="Use Form 6765 to figure the credit.", page=1),
    ]
    md = blocks_to_markdown(blocks)
    assert md.startswith("## Purpose of Form")
    assert "Use Form 6765" in md


def test_heading_depth_is_capped_at_six():
    md = blocks_to_markdown([Block(kind="heading", text="x", page=1, level=99)])
    assert md.startswith("###### x")


# --------------------------------------------------------------------------
# table region exclusion
# --------------------------------------------------------------------------


def test_words_inside_a_table_region_are_dropped():
    """Otherwise table content is indexed twice, doubling its ranking weight."""
    words = [
        {"x0": 10, "x1": 20, "top": 10, "bottom": 20, "text": "inside"},
        {"x0": 200, "x1": 210, "top": 300, "bottom": 310, "text": "outside"},
    ]
    kept = _words_outside(words, [(0, 0, 100, 100)])
    assert [w["text"] for w in kept] == ["outside"]


def test_no_boxes_keeps_everything():
    words = [{"x0": 1, "x1": 2, "top": 1, "bottom": 2, "text": "a"}]
    assert _words_outside(words, []) == words


# --------------------------------------------------------------------------
# table-safe chunking
# --------------------------------------------------------------------------


def test_split_off_tables_separates_prose_and_tables():
    from finance_rag.chunking.hierarchical import split_off_tables

    text = "Intro prose.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nTrailing prose."
    segs = split_off_tables(text)
    kinds = [is_table for is_table, _ in segs]
    assert kinds == [False, True, False]
    assert "| 1 | 2 |" in segs[1][1]


def test_prose_without_tables_is_one_segment():
    from finance_rag.chunking.hierarchical import split_off_tables

    segs = split_off_tables("Just prose.\nMore prose.")
    assert len(segs) == 1 and segs[0][0] is False


def test_table_survives_chunking_intact():
    """A table split mid-way strands rows with no header row.

    The numbers stay readable while the column they belong to disappears.
    """
    from finance_rag.chunking import chunk_document
    from finance_rag.models import DocumentMeta

    rows = "\n".join(f"| {i} | {i * 2}.00% | notes for row {i} |" for i in range(1, 120))
    text = f"# Depreciation\n\nIntro paragraph.\n\n| Year | Rate | Notes |\n|---|---|---|\n{rows}\n"
    meta = DocumentMeta(doc_id="d1", source="t.pdf", title="T")

    chunks = chunk_document(text, meta)
    tables = [c for c in chunks if c.metadata.get("modality") == "table"]
    assert len(tables) == 1
    assert "| Year | Rate | Notes |" in tables[0].text
    assert "| 119 | 238.00% | notes for row 119 |" in tables[0].text


def test_prose_children_are_marked_as_text():
    from finance_rag.chunking import chunk_document
    from finance_rag.models import DocumentMeta

    text = "# Section\n\n" + ("Qualified research expenses include wages. " * 60)
    chunks = chunk_document(text, DocumentMeta(doc_id="d1", source="t.md", title="T"))
    children = [c for c in chunks if c.metadata.get("level") == "child"]
    assert children
    assert all(c.metadata.get("modality") == "text" for c in children)


# --------------------------------------------------------------------------
# end to end on real documents
# --------------------------------------------------------------------------


@requires_corpus
def test_parsing_a_real_pdf_recovers_headings():
    from finance_rag.parsing import parse_pdf

    blocks = parse_pdf(INSTRUCTIONS_PDF)
    headings = [b for b in blocks if b.kind == "heading"]
    assert len(headings) > 20, "layout parsing should recover a real section outline"
    assert any("Purpose of Form" in b.text for b in headings)


@requires_corpus
def test_loader_gives_pdfs_a_section_hierarchy():
    """Regression: PDFs previously produced exactly one section and one parent."""
    from finance_rag.chunking import chunk_document
    from finance_rag.ingestion.loader import load_pdf

    text, meta = load_pdf(INSTRUCTIONS_PDF)
    chunks = chunk_document(text, meta)

    sections = {c.section for c in chunks if c.section}
    parents = [c for c in chunks if c.metadata.get("level") == "parent"]
    children = [c for c in chunks if c.metadata.get("level") == "child"]

    assert len(sections) > 20
    assert len(parents) > 20
    assert sum(1 for c in children if c.section) / len(children) > 0.9


@requires_corpus
def test_loader_falls_back_when_layout_parsing_fails(monkeypatch):
    """A PDF the parser cannot handle must still be indexed, not dropped."""
    from finance_rag import parsing
    from finance_rag.ingestion.loader import _load_pdf_text

    def _boom(*a, **k):
        raise RuntimeError("pdfplumber exploded")

    monkeypatch.setattr(parsing, "parse_pdf", _boom)
    text = _load_pdf_text(SMALL_PDF)
    assert len(text) > 500  # pypdf fallback produced content
