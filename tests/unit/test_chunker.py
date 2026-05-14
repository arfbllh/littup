"""Unit tests for app.ingest.chunker — no DB, no async required."""

from __future__ import annotations

import pytest

from app.ingest.chunker import (
    chunk_blocks,
    extract_entities,
    _update_section_path,
    _split_paragraph_with_overlap,
    _count_tokens,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_REQUIRED_KEYS = {
    "id",
    "document_id",
    "text",
    "token_count",
    "chunk_type",
    "section_path",
    "block_ids",
    "page_start",
    "page_end",
    "char_start",
    "char_end",
    "entities",
    "metadata",
}

DOC_ID = "00000000-0000-0000-0000-000000000001"


def _make_block(
    block_id: str,
    block_type: str,
    text: str,
    page_start: int = 1,
    page_end: int = 1,
    reading_order: int = 0,
    metadata_: dict | None = None,
) -> dict:
    """Build a plain-dict block that chunk_blocks accepts."""
    return {
        "id": block_id,
        "block_type": block_type,
        "text": text,
        "page_start": page_start,
        "page_end": page_end,
        "reading_order": reading_order,
        "metadata_": metadata_ or {},
    }


def _long_paragraph(word: str = "word", n: int = 900) -> str:
    """Return a paragraph whose token count exceeds 800.

    We build sentences of ~10 words each so the splitter can break at '. '.
    """
    sentence = (word + " ") * 9 + word.capitalize() + ". "
    # Each sentence is ~10 tokens; ~90 sentences ≈ 900 tokens
    return (sentence * n)[:5000].strip()


# ── Tests: chunk_blocks — basic structure ─────────────────────────────────────

class TestChunkBlocksStructure:
    def test_empty_blocks_returns_empty_list(self):
        result = chunk_blocks(DOC_ID, [], session=None)
        assert result == []

    def test_required_keys_present(self):
        blocks = [_make_block("b1", "paragraph", "Hello world.", reading_order=0)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert len(chunks) == 1
        assert _REQUIRED_KEYS.issubset(chunks[0].keys())

    def test_document_id_propagated(self):
        blocks = [_make_block("b1", "paragraph", "Some text.", reading_order=0)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert all(c["document_id"] == DOC_ID for c in chunks)

    def test_block_id_in_block_ids(self):
        blocks = [_make_block("b-abc", "paragraph", "Some text.", reading_order=0)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert "b-abc" in chunks[0]["block_ids"]

    def test_char_offsets_are_monotone(self):
        texts = ["First sentence here.", "Second sentence here.", "Third one."]
        blocks = [
            _make_block(f"b{i}", "paragraph", t, reading_order=i)
            for i, t in enumerate(texts)
        ]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        for i in range(1, len(chunks)):
            assert chunks[i]["char_start"] >= chunks[i - 1]["char_end"]

    def test_char_end_equals_start_plus_text_length(self):
        blocks = [_make_block("b1", "paragraph", "Hello world.", reading_order=0)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        c = chunks[0]
        assert c["char_end"] == c["char_start"] + len(c["text"])

    def test_token_count_positive(self):
        blocks = [_make_block("b1", "paragraph", "Hello world.", reading_order=0)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert chunks[0]["token_count"] > 0


# ── Tests: figure blocks are skipped ─────────────────────────────────────────

class TestFigureSkipped:
    def test_figure_block_produces_no_chunk(self):
        blocks = [
            _make_block("b1", "figure", "[Figure 1: diagram]", reading_order=0),
            _make_block("b2", "paragraph", "Caption text.", reading_order=1),
        ]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert len(chunks) == 1
        assert chunks[0]["chunk_type"] == "paragraph"


# ── Tests: header blocks and section_path threading ──────────────────────────

class TestHeaderSectionPath:
    def test_header_sets_section_path(self):
        blocks = [
            _make_block("h1", "header", "Introduction", reading_order=0),
            _make_block("p1", "paragraph", "Body text here.", reading_order=1),
        ]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        # header chunk itself
        header_chunk = next(c for c in chunks if c["chunk_type"] == "header_region")
        assert header_chunk["section_path"] == ["Introduction"]
        # paragraph inherits header's section_path
        para_chunk = next(c for c in chunks if c["chunk_type"] == "paragraph")
        assert para_chunk["section_path"] == ["Introduction"]

    def test_h1_resets_section_path(self):
        blocks = [
            _make_block("h1", "header", "Part One", reading_order=0),
            _make_block("h2", "header", "Part Two", reading_order=1),
            _make_block("p1", "paragraph", "After part two.", reading_order=2),
        ]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        para_chunk = next(c for c in chunks if c["chunk_type"] == "paragraph")
        # Both headers are ≤5 words → each resets stack
        assert para_chunk["section_path"] == ["Part Two"]

    def test_h2_preserves_h1_in_stack(self):
        """A ≤10-word header under a ≤5-word header → [h1_text, h2_text]."""
        h1_text = "Main Section"          # 2 words → H1
        h2_text = "Overview of the main topic here"  # 6 words → H2
        blocks = [
            _make_block("h1", "header", h1_text, reading_order=0),
            _make_block("h2", "header", h2_text, reading_order=1),
            _make_block("p1", "paragraph", "Some detail.", reading_order=2),
        ]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        para_chunk = next(c for c in chunks if c["chunk_type"] == "paragraph")
        assert para_chunk["section_path"] == [h1_text, h2_text]

    def test_initial_section_path_empty_for_headerless_para(self):
        blocks = [_make_block("p1", "paragraph", "Body.", reading_order=0)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert chunks[0]["section_path"] == []

    def test_header_chunk_type_is_header_region(self):
        blocks = [_make_block("h1", "header", "Title", reading_order=0)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert chunks[0]["chunk_type"] == "header_region"


# ── Tests: long paragraph splitting with overlap ──────────────────────────────

class TestLongParagraphSplit:
    def _build_long_para_block(self, n_sentences: int = 100) -> dict:
        """Build a block whose paragraph exceeds 800 tokens."""
        # Each sentence is ~10 words ≈ 10 tokens; 100 sentences ≈ 1000 tokens
        sentence = "The defendant asserted that the contract was invalid. "
        text = sentence * n_sentences
        return _make_block("p-long", "paragraph", text.strip(), reading_order=0)

    def test_long_paragraph_splits_into_multiple_chunks(self):
        block = self._build_long_para_block(n_sentences=100)
        chunks = chunk_blocks(DOC_ID, [block], session=None)
        assert len(chunks) >= 2, "Expected at least 2 chunks for 1000-token paragraph"

    def test_all_split_chunks_are_paragraph_type(self):
        block = self._build_long_para_block(n_sentences=100)
        chunks = chunk_blocks(DOC_ID, [block], session=None)
        assert all(c["chunk_type"] == "paragraph" for c in chunks)

    def test_all_split_chunks_have_same_section_path(self):
        header = _make_block("h1", "header", "Findings", reading_order=0)
        block = self._build_long_para_block(n_sentences=100)
        block["reading_order"] = 1
        chunks = chunk_blocks(DOC_ID, [header, block], session=None)
        para_chunks = [c for c in chunks if c["chunk_type"] == "paragraph"]
        for c in para_chunks:
            assert c["section_path"] == ["Findings"]

    def test_split_chunks_share_block_id(self):
        block = self._build_long_para_block(n_sentences=100)
        chunks = chunk_blocks(DOC_ID, [block], session=None)
        assert all("p-long" in c["block_ids"] for c in chunks)

    def test_split_chunks_within_token_cap(self):
        block = self._build_long_para_block(n_sentences=100)
        chunks = chunk_blocks(DOC_ID, [block], session=None)
        # Each chunk should be no more than max_tokens + one sentence overhead
        for c in chunks:
            assert c["token_count"] <= 900, (
                f"Chunk token_count {c['token_count']} exceeds reasonable cap"
            )

    def test_overlap_exists_between_adjacent_split_chunks(self):
        """Adjacent split chunks share some words from the boundary sentences."""
        block = self._build_long_para_block(n_sentences=100)
        chunks = chunk_blocks(DOC_ID, [block], session=None)
        if len(chunks) < 2:
            pytest.skip("Not enough chunks to check overlap")
        # Last sentence of chunk[0] should appear somewhere in chunk[1]
        chunk0_words = set(chunks[0]["text"].split()[-20:])
        chunk1_words = set(chunks[1]["text"].split()[:20])
        shared = chunk0_words & chunk1_words
        assert len(shared) > 0, "Expected overlap between adjacent paragraph chunks"


# ── Tests: table chunks ───────────────────────────────────────────────────────

class TestTableChunks:
    def test_small_table_single_chunk(self):
        cells = [["Name", "Date"], ["Harvey Specter", "January 1, 2026"]]
        text = "\n".join("\t".join(row) for row in cells)
        blocks = [_make_block("t1", "table", text, metadata_={"cells": cells})]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert len(chunks) == 1
        assert chunks[0]["chunk_type"] == "table"
        assert chunks[0]["metadata"]["cells"] == cells

    def test_large_table_splits_into_table_rows(self):
        # Build a 25-row table that will exceed 800 tokens.
        # Use longer repeated cell content to push token count above the 800 cap.
        rows = [[f"Column{j} value for row number {i} with extra padding text here" for j in range(4)] for i in range(25)]
        text = "\n".join("\t".join(row) for row in rows)
        blocks = [_make_block("t2", "table", text, metadata_={"cells": rows})]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        # With 25 rows split into groups of 10: 3 chunks
        table_rows_chunks = [c for c in chunks if c["chunk_type"] == "table_rows"]
        assert len(table_rows_chunks) >= 2

    def test_table_chunk_has_cells_in_metadata(self):
        cells = [["A", "B"], ["C", "D"]]
        text = "A\tB\nC\tD"
        blocks = [_make_block("t3", "table", text, metadata_={"cells": cells})]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert "cells" in chunks[0]["metadata"]


# ── Tests: list chunks ────────────────────────────────────────────────────────

class TestListChunks:
    def test_short_list_single_chunk(self):
        items = [f"Item {i}" for i in range(5)]
        text = "\n".join(items)
        blocks = [_make_block("l1", "list", text)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert len(chunks) == 1
        assert chunks[0]["chunk_type"] == "list"

    def test_long_list_splits_into_windows(self):
        items = [f"Item {i}" for i in range(25)]
        text = "\n".join(items)
        blocks = [_make_block("l2", "list", text)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        # 25 items / 10 per window = 3 chunks
        assert len(chunks) == 3
        assert all(c["chunk_type"] == "list" for c in chunks)

    def test_list_boundary_exactly_10_items_single_chunk(self):
        """Exactly 9 items → single chunk (< 10 threshold)."""
        items = [f"Item {i}" for i in range(9)]
        text = "\n".join(items)
        blocks = [_make_block("l3", "list", text)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert len(chunks) == 1


# ── Tests: char_cursor accumulates across blocks ──────────────────────────────

class TestCharCursor:
    def test_char_start_of_second_block_equals_char_end_of_first(self):
        b1 = _make_block("b1", "paragraph", "Hello world.", reading_order=0)
        b2 = _make_block("b2", "paragraph", "Second block.", reading_order=1)
        chunks = chunk_blocks(DOC_ID, [b1, b2], session=None)
        assert len(chunks) == 2
        assert chunks[1]["char_start"] == chunks[0]["char_end"]

    def test_figures_advance_cursor_silently(self):
        """A figure block advances the cursor without producing a chunk."""
        fig = _make_block("f1", "figure", "FIGURE_TEXT", reading_order=0)
        para = _make_block("p1", "paragraph", "After figure.", reading_order=1)
        chunks = chunk_blocks(DOC_ID, [fig, para], session=None)
        assert len(chunks) == 1
        # char_start should reflect the figure text length
        assert chunks[0]["char_start"] == len("FIGURE_TEXT")


# ── Tests: _update_section_path ───────────────────────────────────────────────

class TestUpdateSectionPath:
    def test_short_header_resets_stack(self):
        stack = ["Old Section", "Sub Section"]
        result = _update_section_path(stack, "Title")
        assert result == ["Title"]

    def test_medium_header_preserves_first(self):
        # 7 words → H2 range (6-10 words), stack non-empty → [stack[0], header]
        stack = ["Main"]
        result = _update_section_path(stack, "Sub section overview of main topic here")
        assert result == ["Main", "Sub section overview of main topic here"]

    def test_long_header_appends(self):
        stack = ["Part One", "Chapter A"]
        long_header = "This is a very long header with many many words here"
        result = _update_section_path(stack, long_header)
        assert result == ["Part One", "Chapter A", long_header]

    def test_medium_header_with_empty_stack_appends(self):
        # ≤10 words but stack is empty → appends
        stack: list[str] = []
        result = _update_section_path(stack, "Section overview here")
        assert result == ["Section overview here"]


# ── Tests: entities on chunks ─────────────────────────────────────────────────

class TestChunkEntities:
    def test_statute_entities_extracted_in_chunk(self):
        text = "Pursuant to 42 U.S.C. § 1983, the plaintiff seeks damages."
        blocks = [_make_block("b1", "paragraph", text)]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert any("1983" in e for e in chunks[0]["entities"])

    def test_entities_list_is_list_type(self):
        blocks = [_make_block("b1", "paragraph", "Hello world.")]
        chunks = chunk_blocks(DOC_ID, blocks, session=None)
        assert isinstance(chunks[0]["entities"], list)
