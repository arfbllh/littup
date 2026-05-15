"""Layout parsing — converts OCR spans into semantic blocks (paragraphs, headers, tables, etc.).

State transition: ocr_done → layout_running → layout_done (or failed).
"""

from __future__ import annotations

import asyncio
import json
import statistics
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_uuid7
from app.ingest.events import DocumentEventBus

logger = structlog.get_logger(__name__)

# Tuning constants for span-based block grouper
_LINE_TOLERANCE = 4.0        # px: vertical distance within which spans are on the same line
_PARA_GAP_RATIO = 1.5        # gap > this × median_line_height → new paragraph
_HEADER_HEIGHT_RATIO = 1.35  # span height > this × median → treat as header
_MIN_HEADER_TOKENS = 12      # headers are short; more tokens → demote to paragraph
_COLUMN_GAP_FRACTION = 0.2   # page-width fraction that must separate two x-midpoint clusters


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class _RawSpan:
    span_id: str
    page_number: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def x_mid(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.0)


@dataclass
class _Block:
    id: str
    block_type: str  # paragraph | header | table | list | figure
    text: str
    page_start: int
    page_end: int
    reading_order: int
    x0: float
    y0: float
    x1: float
    y1: float
    metadata: dict = field(default_factory=dict)
    span_ids: list[str] = field(default_factory=list)


# ── Public entry point ────────────────────────────────────────────────────────

async def parse_layout(document_id: str, session: AsyncSession) -> dict[str, Any]:
    """Drive layout parsing for one document.

    Returns a summary dict used as the job result payload.
    Idempotent: multiple workers competing for the same doc only one wins the
    atomic claim.
    """
    claimed = await _claim_layout_running(document_id, session)
    if not claimed:
        return {"skipped": True, "reason": "already_claimed"}

    await DocumentEventBus.emit(
        session, document_id, "status_changed", {"to": "layout_running"}
    )
    await session.commit()

    try:
        spans = await _load_spans(document_id, session)

        doc_row = await session.execute(
            text("SELECT sha256, mime_type, page_count FROM app.documents WHERE id = :id"),
            {"id": document_id},
        )
        doc = doc_row.fetchone()

        # WS-A.3: if pages were rendered but OCR pulled nothing, fail loudly
        # instead of silently advancing to `ready` with zero blocks.
        if not spans:
            page_count = int((doc.page_count if doc is not None else 0) or 0)
            if page_count > 0:
                await session.execute(
                    text(
                        """
                        UPDATE app.documents
                           SET status = 'failed',
                               error_code = 'EMPTY_OCR_OUTPUT',
                               error_message = :msg,
                               updated_at = NOW()
                         WHERE id = :id
                        """
                    ),
                    {
                        "id": document_id,
                        "msg": (
                            f"OCR produced no spans across {page_count} page(s); "
                            "likely a scanned PDF without a usable text layer."
                        ),
                    },
                )
                await DocumentEventBus.emit(
                    session,
                    document_id,
                    "failed",
                    {
                        "error_code": "EMPTY_OCR_OUTPUT",
                        "page_count": page_count,
                        "span_count": 0,
                    },
                )
                await session.commit()
                logger.warning(
                    "layout_empty_spans",
                    document_id=document_id,
                    page_count=page_count,
                )
                return {
                    "document_id": document_id,
                    "block_count": 0,
                    "failed": True,
                    "error_code": "EMPTY_OCR_OUTPUT",
                }
            # No spans AND no pages — let the empty-blocks path complete; this
            # is a degenerate input that callers can treat as ready-empty.
            await _transition_layout_done(document_id, session, blocks=[])
            return {"document_id": document_id, "block_count": 0}

        loop = asyncio.get_running_loop()
        blocks = await loop.run_in_executor(None, _parse_blocks_sync, spans, doc)

        blocks = _post_process(blocks)
        await _transition_layout_done(document_id, session, blocks=blocks)

        logger.info(
            "layout_done",
            document_id=document_id,
            block_count=len(blocks),
        )
        return {"document_id": document_id, "block_count": len(blocks)}

    except Exception as exc:
        logger.exception("layout_failed", document_id=document_id, error=str(exc))
        await session.execute(
            text(
                """
                UPDATE app.documents
                   SET status = 'failed',
                       error_code = 'LAYOUT_ERROR',
                       error_message = :msg,
                       updated_at = NOW()
                 WHERE id = :id
                """
            ),
            {"id": document_id, "msg": str(exc)},
        )
        await DocumentEventBus.emit(
            session, document_id, "failed",
            {"error_code": "LAYOUT_ERROR", "error_message": str(exc)},
        )
        await session.commit()
        raise


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _claim_layout_running(document_id: str, session: AsyncSession) -> bool:
    result = await session.execute(
        text(
            """
            UPDATE app.documents
               SET status = 'layout_running', updated_at = NOW()
             WHERE id = :id AND status = 'ocr_done'
            RETURNING id
            """
        ),
        {"id": document_id},
    )
    return result.rowcount > 0


async def _load_spans(document_id: str, session: AsyncSession) -> list[_RawSpan]:
    result = await session.execute(
        text(
            """
            SELECT s.id, s.text, s.bbox_x0, s.bbox_y0, s.bbox_x1, s.bbox_y1,
                   p.page_number
            FROM app.spans s
            JOIN app.pages p ON s.page_id = p.id
            WHERE p.document_id = :doc_id
            ORDER BY p.page_number, s.bbox_y0, s.bbox_x0
            """
        ),
        {"doc_id": document_id},
    )
    return [
        _RawSpan(
            span_id=str(r.id),
            page_number=r.page_number,
            text=r.text,
            x0=float(r.bbox_x0 or 0.0),
            y0=float(r.bbox_y0 or 0.0),
            x1=float(r.bbox_x1 or 0.0),
            y1=float(r.bbox_y1 or 0.0),
        )
        for r in result.fetchall()
    ]


async def _transition_layout_done(
    document_id: str, session: AsyncSession, blocks: list[_Block]
) -> None:
    if blocks:
        block_rows = [
            {
                "id": b.id,
                "document_id": document_id,
                "block_type": b.block_type,
                "text": b.text,
                "bbox_x0": b.x0,
                "bbox_y0": b.y0,
                "bbox_x1": b.x1,
                "bbox_y1": b.y1,
                "reading_order": b.reading_order,
                "page_start": b.page_start,
                "page_end": b.page_end,
                "metadata": json.dumps(b.metadata),
            }
            for b in blocks
        ]
        await session.execute(
            text(
                """
                INSERT INTO app.blocks
                       (id, document_id, block_type, text, bbox_x0, bbox_y0, bbox_x1, bbox_y1,
                        reading_order, page_start, page_end, metadata)
                VALUES (:id, :document_id, :block_type, :text, :bbox_x0, :bbox_y0, :bbox_x1,
                        :bbox_y1, :reading_order, :page_start, :page_end,
                        CAST(:metadata AS jsonb))
                """
            ),
            block_rows,
        )

        # Update spans.block_id — one UPDATE per block, matching bbox overlap
        for b in blocks:
            await session.execute(
                text(
                    """
                    UPDATE app.spans s SET block_id = :block_id
                    FROM app.pages p
                    WHERE s.page_id = p.id
                      AND p.page_number BETWEEN :page_start AND :page_end
                      AND s.bbox_x0 >= :bx0 AND s.bbox_y0 >= :by0
                      AND s.bbox_x1 <= :bx1 AND s.bbox_y1 <= :by1
                    """
                ),
                {
                    "block_id": b.id,
                    "page_start": b.page_start,
                    "page_end": b.page_end,
                    "bx0": b.x0,
                    "by0": b.y0,
                    "bx1": b.x1,
                    "by1": b.y1,
                },
            )

    await session.execute(
        text(
            "UPDATE app.documents SET status = 'layout_done', updated_at = NOW() WHERE id = :id"
        ),
        {"id": document_id},
    )
    await DocumentEventBus.emit(
        session, document_id, "status_changed", {"to": "layout_done"}
    )
    await session.commit()


# ── Parsing logic (runs in thread executor) ───────────────────────────────────

def _parse_blocks_sync(spans: list[_RawSpan], doc_row: Any) -> list[_Block]:
    """Try docling → unstructured → span grouper. Always returns something."""
    if doc_row is not None:
        try:
            from app.ingest.mime import ext_for
            from app.ingest.storage import LocalBlobStore

            ext = ext_for(doc_row.mime_type or "application/pdf")
            file_path = LocalBlobStore().path_for(doc_row.sha256, ext)
            if file_path.exists():
                return _parse_with_docling(str(file_path), spans)
        except ImportError:
            logger.info("docling_not_installed")
        except Exception as exc:
            logger.warning("docling_failed_falling_back", error=str(exc))

        try:
            from app.ingest.mime import ext_for
            from app.ingest.storage import LocalBlobStore

            ext = ext_for(doc_row.mime_type or "application/pdf")
            file_path = LocalBlobStore().path_for(doc_row.sha256, ext)
            if file_path.exists():
                return _parse_with_unstructured(str(file_path), spans)
        except ImportError:
            logger.info("unstructured_not_installed")
        except Exception as exc:
            logger.warning("unstructured_failed_falling_back", error=str(exc))

    return _group_spans_into_blocks(spans)


def _parse_with_docling(file_path: str, spans: list[_RawSpan]) -> list[_Block]:
    from docling.document_converter import DocumentConverter  # noqa: PLC0415

    converter = DocumentConverter()
    result = converter.convert(source=file_path)
    doc = result.document

    blocks: list[_Block] = []
    reading_order = 0

    for item, _level in doc.iterate_items():
        label = getattr(item, "label", None) or ""
        text = getattr(item, "text", None) or ""
        if not text.strip():
            continue

        # Map docling label → block_type
        if "section" in str(label).lower() or "title" in str(label).lower():
            block_type = "header"
        elif "table" in str(label).lower():
            block_type = "table"
        elif "list" in str(label).lower():
            block_type = "list"
        elif "figure" in str(label).lower():
            block_type = "figure"
        else:
            block_type = "paragraph"

        # Extract bbox from provenance
        prov = getattr(item, "prov", None) or []
        x0, y0, x1, y1 = 0.0, 0.0, 0.0, 0.0
        page_start, page_end = 1, 1
        metadata: dict[str, Any] = {}

        if prov:
            p = prov[0]
            page_start = page_end = getattr(p, "page_no", 1) or 1
            bbox = getattr(p, "bbox", None)
            if bbox is not None:
                x0 = float(getattr(bbox, "l", 0.0))
                y0 = float(getattr(bbox, "t", 0.0))
                x1 = float(getattr(bbox, "r", 0.0))
                y1 = float(getattr(bbox, "b", 0.0))

        # Tables: extract cells 2D array
        if block_type == "table":
            try:
                cells = _extract_docling_table_cells(item)
                if cells:
                    metadata["cells"] = cells
            except Exception:
                pass

        blocks.append(
            _Block(
                id=new_uuid7(),
                block_type=block_type,
                text=text.strip(),
                page_start=page_start,
                page_end=page_end,
                reading_order=reading_order,
                x0=x0, y0=y0, x1=x1, y1=y1,
                metadata=metadata,
            )
        )
        reading_order += 1

    if not blocks:
        return _group_spans_into_blocks(spans)
    return blocks


def _extract_docling_table_cells(item: Any) -> list[list[str]]:
    """Return 2D cell array from a docling TableItem."""
    data = getattr(item, "data", None)
    if data is None:
        return []
    grid = getattr(data, "grid", None) or getattr(data, "table_cells", None)
    if grid is None:
        return []
    result: list[list[str]] = []
    for row in grid:
        result.append([getattr(cell, "text", str(cell)) for cell in row])
    return result


def _parse_with_unstructured(file_path: str, spans: list[_RawSpan]) -> list[_Block]:
    from unstructured.partition.pdf import partition_pdf  # noqa: PLC0415

    elements = partition_pdf(filename=file_path)
    if not elements:
        return _group_spans_into_blocks(spans)

    _CATEGORY_MAP = {
        "Title": "header",
        "Header": "header",
        "NarrativeText": "paragraph",
        "Table": "table",
        "ListItem": "list",
        "Image": "figure",
        "FigureCaption": "figure",
    }

    blocks: list[_Block] = []
    reading_order = 0

    for el in elements:
        text = getattr(el, "text", "") or ""
        if not text.strip():
            continue
        category = getattr(el, "category", "NarrativeText") or "NarrativeText"
        block_type = _CATEGORY_MAP.get(category, "paragraph")

        # Try to get coordinates
        x0, y0, x1, y1 = 0.0, 0.0, 0.0, 0.0
        page_start = page_end = 1
        meta = getattr(el, "metadata", None)
        if meta:
            coords = getattr(meta, "coordinates", None)
            if coords:
                pts = getattr(coords, "points", None) or []
                if len(pts) >= 2:
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            page_num = getattr(meta, "page_number", None)
            if page_num is not None:
                page_start = page_end = int(page_num)

        blocks.append(
            _Block(
                id=new_uuid7(),
                block_type=block_type,
                text=text.strip(),
                page_start=page_start,
                page_end=page_end,
                reading_order=reading_order,
                x0=x0, y0=y0, x1=x1, y1=y1,
            )
        )
        reading_order += 1

    if not blocks:
        return _group_spans_into_blocks(spans)
    return blocks


# ── Span-based block grouper (reliable fallback) ──────────────────────────────

def _group_spans_into_blocks(spans: list[_RawSpan]) -> list[_Block]:
    """Group OCR spans into semantic blocks without any external dependency."""
    if not spans:
        return []

    # Group by page
    by_page: dict[int, list[_RawSpan]] = {}
    for s in spans:
        by_page.setdefault(s.page_number, []).append(s)

    all_blocks: list[_Block] = []
    reading_order = 0

    for page_num in sorted(by_page):
        page_spans = sorted(by_page[page_num], key=lambda s: (s.y0, s.x0))
        if not page_spans:
            continue

        # Compute median span height for the page (font-size proxy)
        heights = [s.height for s in page_spans if s.height > 1.0]
        median_h = statistics.median(heights) if heights else 12.0

        # Detect multi-column layout by x-midpoint bimodal clustering
        column_split = _detect_column_split(page_spans)

        # Group spans into lines
        lines = _group_into_lines(page_spans, _LINE_TOLERANCE)

        # Assign column bucket to each line
        if column_split is not None:
            for line in lines:
                mid = sum(s.x_mid for s in line) / len(line)
                for s in line:
                    s._col = 0 if mid < column_split else 1  # type: ignore[attr-defined]
        else:
            for line in lines:
                for s in line:
                    s._col = 0  # type: ignore[attr-defined]

        # Re-sort lines with column bucket if multi-column
        if column_split is not None:
            lines.sort(key=lambda ln: (getattr(ln[0], "_col", 0), ln[0].y0))

        # Group lines into text blocks (separated by gaps)
        text_blocks = _group_lines_into_blocks(lines, median_h)

        for raw_block in text_blocks:
            flat_spans = [s for line in raw_block for s in line]
            block_text = " ".join(s.text for s in flat_spans).strip()
            if not block_text:
                continue

            bx0 = min(s.x0 for s in flat_spans)
            by0 = min(s.y0 for s in flat_spans)
            bx1 = max(s.x1 for s in flat_spans)
            by1 = max(s.y1 for s in flat_spans)

            avg_span_h = sum(s.height for s in flat_spans) / max(len(flat_spans), 1)
            token_count = len(block_text.split())
            is_tall = avg_span_h > _HEADER_HEIGHT_RATIO * median_h
            block_type = "header" if (is_tall and token_count <= _MIN_HEADER_TOKENS) else "paragraph"

            all_blocks.append(
                _Block(
                    id=new_uuid7(),
                    block_type=block_type,
                    text=block_text,
                    page_start=page_num,
                    page_end=page_num,
                    reading_order=reading_order,
                    x0=bx0, y0=by0, x1=bx1, y1=by1,
                    span_ids=[s.span_id for s in flat_spans],
                )
            )
            reading_order += 1

    return all_blocks


def _group_into_lines(
    spans: list[_RawSpan], tolerance: float
) -> list[list[_RawSpan]]:
    """Group spans into lines based on vertical proximity."""
    if not spans:
        return []
    lines: list[list[_RawSpan]] = [[spans[0]]]
    for span in spans[1:]:
        last_line = lines[-1]
        last_y0 = sum(s.y0 for s in last_line) / len(last_line)
        if abs(span.y0 - last_y0) <= tolerance:
            last_line.append(span)
        else:
            lines.append([span])
    return lines


def _group_lines_into_blocks(
    lines: list[list[_RawSpan]], median_h: float
) -> list[list[list[_RawSpan]]]:
    """Group lines into paragraph blocks by vertical gap."""
    if not lines:
        return []
    threshold = _PARA_GAP_RATIO * max(median_h, 4.0)
    blocks: list[list[list[_RawSpan]]] = [[lines[0]]]
    for i in range(1, len(lines)):
        prev_line = lines[i - 1]
        curr_line = lines[i]
        prev_col = getattr(prev_line[0], "_col", 0)
        curr_col = getattr(curr_line[0], "_col", 0)
        if curr_col != prev_col:
            blocks.append([curr_line])
            continue
        prev_y1 = max(s.y1 for s in prev_line)
        curr_y0 = min(s.y0 for s in curr_line)
        gap = curr_y0 - prev_y1
        if gap > threshold:
            blocks.append([curr_line])
        else:
            blocks[-1].append(curr_line)
    return blocks


def _detect_column_split(spans: list[_RawSpan]) -> float | None:
    """Return the x coordinate splitting left/right columns, or None if single-column."""
    if len(spans) < 10:
        return None
    mids = sorted(s.x_mid for s in spans)
    x_min, x_max = mids[0], mids[-1]
    page_width = max(x_max - x_min, 1.0)

    # Scan for a gap in x_midpoints that exceeds _COLUMN_GAP_FRACTION of page width
    gap_threshold = _COLUMN_GAP_FRACTION * page_width
    for i in range(1, len(mids)):
        if mids[i] - mids[i - 1] > gap_threshold:
            left_mid = (x_min + mids[i - 1]) / 2.0
            right_mid = (mids[i] + x_max) / 2.0
            # Only split if each cluster spans a meaningful width
            if (mids[i - 1] - x_min) > 0.1 * page_width and (x_max - mids[i]) > 0.1 * page_width:
                return (mids[i - 1] + mids[i]) / 2.0
    return None


# ── Post-processing ───────────────────────────────────────────────────────────

def _post_process(blocks: list[_Block]) -> list[_Block]:
    """Apply post-processing rules in order."""
    blocks = [b for b in blocks if b.text.strip()]
    blocks = _merge_orphan_singles(blocks)
    # Re-assign reading_order after any merges/drops
    for i, b in enumerate(blocks):
        b.reading_order = i
    return blocks


def _merge_orphan_singles(blocks: list[_Block]) -> list[_Block]:
    """Merge single-word/token paragraphs into the preceding block."""
    if len(blocks) < 2:
        return blocks
    result: list[_Block] = [blocks[0]]
    for b in blocks[1:]:
        is_orphan = (
            b.block_type == "paragraph"
            and len(b.text.split()) == 1
            and result
            and result[-1].block_type in ("paragraph", "header")
        )
        if is_orphan:
            prev = result[-1]
            prev.text = prev.text.rstrip() + " " + b.text
            prev.y1 = max(prev.y1, b.y1)
            prev.x0 = min(prev.x0, b.x0)
            prev.x1 = max(prev.x1, b.x1)
            prev.page_end = max(prev.page_end, b.page_end)
            prev.span_ids.extend(b.span_ids)
        else:
            result.append(b)
    return result
