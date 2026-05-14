"""Semantic chunker — converts layout blocks into chunk dicts ready for DB insert.

This module is a pure function library: no async, no DB access.
The caller (ingest/service.py) is responsible for INSERTing the returned dicts.

State transition: layout_done → chunking_running → chunking_done (or failed).
"""

from __future__ import annotations

import re
from typing import Any

from app.core.ids import new_uuid7

# ── Token counting ────────────────────────────────────────────────────────────

def _get_encoder():
    """Lazily load tiktoken; fall back to a word-count heuristic if unavailable."""
    try:
        import tiktoken  # noqa: PLC0415
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _count_tokens(text: str, enc: Any) -> int:
    if enc is None:
        return int(len(text.split()) * 1.3)
    return len(enc.encode(text))


# ── Entity extraction ─────────────────────────────────────────────────────────

PATTERNS: list[str] = [
    # US statute citations: "42 U.S.C. § 1983" or "18 USC 1343"
    r'\b\d+\s+U\.?S\.?C\.?\s*§?\s*\d+(?:\([a-z]\))?\b',
    # Case citations: "Brown v. Board"
    r'\b[A-Z]\w+\s+v\.\s+[A-Z]\w+\b',
    # Proper noun runs (1-3 capitalized words followed by one more): "Harvey Specter"
    r'(?<!\.\s)\b(?:[A-Z][a-z]+\s){1,3}[A-Z][a-z]+\b',
    # Dollar amounts: "$1,500,000" or "$75.00"
    r'\$[\d,]+(?:\.\d{2})?',
    # Full dates: "January 15, 2026"
    r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',
]

_COMPILED_PATTERNS: list[re.Pattern] = [re.compile(p) for p in PATTERNS]


def extract_entities(text: str) -> list[str]:
    """Extract named entities from text using regex patterns.

    Returns a deduplicated list preserving first-occurrence order.
    """
    seen: dict[str, None] = {}  # ordered set via insertion-ordered dict
    for pattern in _COMPILED_PATTERNS:
        for m in pattern.finditer(text):
            entity = m.group(0).strip()
            if entity and entity not in seen:
                seen[entity] = None
    return list(seen.keys())


# ── Section path management ───────────────────────────────────────────────────

def _update_section_path(current_stack: list[str], header_text: str) -> list[str]:
    """Return a new section_path stack given a new header block text.

    Heuristic:
    - H1 (≤5 words) → reset to [header_text]
    - H2 (≤10 words) and stack non-empty → [stack[0], header_text]
    - Otherwise → [*current_stack, header_text]
    """
    word_count = len(header_text.split())
    if word_count <= 5:
        return [header_text]
    elif word_count <= 10 and current_stack:
        return [current_stack[0], header_text]
    else:
        return list(current_stack) + [header_text]


# ── Sentence-boundary splitter ────────────────────────────────────────────────

def _split_at_sentences(text: str) -> list[str]:
    """Split text into sentences at '. ' followed by an uppercase letter."""
    # Find all positions where we can split: '. ' + capital
    split_positions = [0]
    for m in re.finditer(r'\.\s+(?=[A-Z])', text):
        # Split after the period (include it in the left chunk)
        split_positions.append(m.end())
    split_positions.append(len(text))

    sentences: list[str] = []
    for i in range(len(split_positions) - 1):
        segment = text[split_positions[i]:split_positions[i + 1]].strip()
        if segment:
            sentences.append(segment)
    return sentences


def _split_paragraph_with_overlap(
    text: str, max_tokens: int, overlap_tokens: int, enc: Any
) -> list[str]:
    """Split a long paragraph into overlapping chunks at sentence boundaries.

    Each chunk is at most max_tokens tokens; adjacent chunks share overlap_tokens
    worth of text from the end of the previous chunk.
    """
    sentences = _split_at_sentences(text)
    if not sentences:
        return [text]

    chunks: list[str] = []
    current_sentences: list[str] = []
    current_tokens = 0

    i = 0
    while i < len(sentences):
        sentence = sentences[i]
        s_tokens = _count_tokens(sentence, enc)

        if current_tokens + s_tokens <= max_tokens:
            current_sentences.append(sentence)
            current_tokens += s_tokens
            i += 1
        else:
            if current_sentences:
                chunks.append(" ".join(current_sentences))
                # Compute overlap: take sentences from the tail until we reach
                # overlap_tokens budget
                overlap_sents: list[str] = []
                overlap_total = 0
                for sent in reversed(current_sentences):
                    t = _count_tokens(sent, enc)
                    if overlap_total + t <= overlap_tokens:
                        overlap_sents.insert(0, sent)
                        overlap_total += t
                    else:
                        break
                current_sentences = overlap_sents
                current_tokens = overlap_total
            else:
                # Single sentence too long — emit as-is
                chunks.append(sentence)
                current_sentences = []
                current_tokens = 0
                i += 1

    if current_sentences:
        chunks.append(" ".join(current_sentences))

    return chunks if chunks else [text]


# ── Block attribute accessor ──────────────────────────────────────────────────

def _attr(block: Any, name: str, default: Any = None) -> Any:
    """Access block attribute by name, supporting both ORM rows and plain dicts."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


# ── Main chunker ─────────────────────────────────────────────────────────────

_MAX_TOKENS = 800
_OVERLAP_TOKENS = 50
_TABLE_ROW_GROUP = 10
_LIST_WINDOW = 10


def chunk_blocks(
    document_id: str,
    blocks: list[Any],
    session: Any = None,  # not used; caller does the INSERT
) -> list[dict]:
    """Convert layout blocks into chunk dicts.

    Parameters
    ----------
    document_id:
        UUID of the owning document.
    blocks:
        Ordered list of block objects (ORM rows or plain dicts) with fields:
        id, block_type, text, page_start, page_end, reading_order, metadata_
    session:
        Accepted for API compatibility but not used — this function is pure.

    Returns
    -------
    List of chunk dicts ready for bulk INSERT into app.chunks.
    """
    enc = _get_encoder()
    section_path: list[str] = []
    char_cursor: int = 0
    chunks: list[dict] = []

    for block in blocks:
        block_id = str(_attr(block, "id", ""))
        block_type = _attr(block, "block_type", "paragraph")
        text = _attr(block, "text", "") or ""
        page_start = _attr(block, "page_start", 1) or 1
        page_end = _attr(block, "page_end", page_start) or page_start
        metadata_ = _attr(block, "metadata_", {}) or {}

        if not text.strip():
            char_cursor += len(text)
            continue

        if block_type == "figure":
            # Skip figures entirely
            char_cursor += len(text)
            continue

        elif block_type == "header":
            section_path = _update_section_path(section_path, text.strip())
            chunk = _make_chunk(
                document_id=document_id,
                text=text,
                chunk_type="header_region",
                section_path=list(section_path),
                block_ids=[block_id],
                page_start=page_start,
                page_end=page_end,
                char_cursor=char_cursor,
                enc=enc,
                metadata={},
            )
            chunks.append(chunk)
            char_cursor = chunk["char_end"]

        elif block_type == "paragraph":
            token_count = _count_tokens(text, enc)
            if token_count <= _MAX_TOKENS:
                chunk = _make_chunk(
                    document_id=document_id,
                    text=text,
                    chunk_type="paragraph",
                    section_path=list(section_path),
                    block_ids=[block_id],
                    page_start=page_start,
                    page_end=page_end,
                    char_cursor=char_cursor,
                    enc=enc,
                    metadata={},
                )
                chunks.append(chunk)
                char_cursor = chunk["char_end"]
            else:
                # Split into overlapping sentence-boundary chunks
                splits = _split_paragraph_with_overlap(
                    text, _MAX_TOKENS, _OVERLAP_TOKENS, enc
                )
                for split_text in splits:
                    chunk = _make_chunk(
                        document_id=document_id,
                        text=split_text,
                        chunk_type="paragraph",
                        section_path=list(section_path),
                        block_ids=[block_id],
                        page_start=page_start,
                        page_end=page_end,
                        char_cursor=char_cursor,
                        enc=enc,
                        metadata={},
                    )
                    chunks.append(chunk)
                    char_cursor = chunk["char_end"]

        elif block_type == "table":
            cells = metadata_.get("cells", [])
            token_count = _count_tokens(text, enc)
            if token_count <= _MAX_TOKENS:
                chunk = _make_chunk(
                    document_id=document_id,
                    text=text,
                    chunk_type="table",
                    section_path=list(section_path),
                    block_ids=[block_id],
                    page_start=page_start,
                    page_end=page_end,
                    char_cursor=char_cursor,
                    enc=enc,
                    metadata={"cells": cells},
                )
                chunks.append(chunk)
                char_cursor = chunk["char_end"]
            else:
                # Split rows into groups of ~_TABLE_ROW_GROUP
                rows = cells if cells else [
                    [line] for line in text.split("\n") if line.strip()
                ]
                row_groups = [
                    rows[i: i + _TABLE_ROW_GROUP]
                    for i in range(0, max(len(rows), 1), _TABLE_ROW_GROUP)
                ]
                for group in row_groups:
                    group_text = "\n".join(
                        "\t".join(str(cell) for cell in row) for row in group
                    )
                    chunk = _make_chunk(
                        document_id=document_id,
                        text=group_text,
                        chunk_type="table_rows",
                        section_path=list(section_path),
                        block_ids=[block_id],
                        page_start=page_start,
                        page_end=page_end,
                        char_cursor=char_cursor,
                        enc=enc,
                        metadata={"cells": group},
                    )
                    chunks.append(chunk)
                    char_cursor = chunk["char_end"]

        elif block_type == "list":
            items = [line for line in text.split("\n") if line.strip()]
            if len(items) < _LIST_WINDOW:
                chunk = _make_chunk(
                    document_id=document_id,
                    text=text,
                    chunk_type="list",
                    section_path=list(section_path),
                    block_ids=[block_id],
                    page_start=page_start,
                    page_end=page_end,
                    char_cursor=char_cursor,
                    enc=enc,
                    metadata={},
                )
                chunks.append(chunk)
                char_cursor = chunk["char_end"]
            else:
                # Sliding windows of ~_LIST_WINDOW items
                windows = [
                    items[i: i + _LIST_WINDOW]
                    for i in range(0, len(items), _LIST_WINDOW)
                ]
                for window in windows:
                    window_text = "\n".join(window)
                    chunk = _make_chunk(
                        document_id=document_id,
                        text=window_text,
                        chunk_type="list",
                        section_path=list(section_path),
                        block_ids=[block_id],
                        page_start=page_start,
                        page_end=page_end,
                        char_cursor=char_cursor,
                        enc=enc,
                        metadata={},
                    )
                    chunks.append(chunk)
                    char_cursor = chunk["char_end"]

        else:
            # Unknown block type — treat as paragraph
            chunk = _make_chunk(
                document_id=document_id,
                text=text,
                chunk_type="paragraph",
                section_path=list(section_path),
                block_ids=[block_id],
                page_start=page_start,
                page_end=page_end,
                char_cursor=char_cursor,
                enc=enc,
                metadata={},
            )
            chunks.append(chunk)
            char_cursor = chunk["char_end"]

    return chunks


def _make_chunk(
    *,
    document_id: str,
    text: str,
    chunk_type: str,
    section_path: list[str],
    block_ids: list[str],
    page_start: int,
    page_end: int,
    char_cursor: int,
    enc: Any,
    metadata: dict,
) -> dict:
    """Build a single chunk dict with all required fields."""
    char_start = char_cursor
    char_end = char_cursor + len(text)
    return {
        "id": new_uuid7(),
        "document_id": document_id,
        "text": text,
        "token_count": _count_tokens(text, enc),
        "chunk_type": chunk_type,
        "section_path": section_path,
        "block_ids": block_ids,
        "page_start": page_start,
        "page_end": page_end,
        "char_start": char_start,
        "char_end": char_end,
        "entities": extract_entities(text),
        "metadata": metadata,
    }
