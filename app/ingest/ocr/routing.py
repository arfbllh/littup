"""OCR routing — classify page → select provider → call; enforce NN-6 VLM cap."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BudgetExceededError
from app.ingest.classifier import PageType, classify_page
from app.ingest.ocr.base import OCRConfig, PageExtraction, load_ocr_config

if TYPE_CHECKING:
    from app.llm.router import LLMRouter

logger = structlog.get_logger(__name__)


async def route_and_extract(
    *,
    page_image: np.ndarray,
    source_path: Path,
    page_num: int,
    has_text_layer: bool,
    doc_id: str,
    session: AsyncSession,
    config: OCRConfig | None = None,
    llm_router: LLMRouter | None = None,
) -> PageExtraction:
    """Classify the page, select the best provider, and return a PageExtraction.

    VLM escalation (NN-6): the per-document vlm_pages_used counter is
    incremented atomically before any VLM call.  If already at the cap,
    the returned extraction carries source='vlm_budget_exceeded'.
    """
    cfg = config or load_ocr_config()
    page_type, clf_conf = classify_page(page_image, has_text_layer=has_text_layer, config=cfg)

    logger.debug(
        "ocr_routing",
        doc_id=doc_id,
        page=page_num,
        page_type=page_type,
        clf_conf=round(clf_conf, 3),
    )

    # ── Native PDF → pdfplumber (no rasterisation needed) ─────────────────────
    if page_type == PageType.NATIVE:
        from app.ingest.ocr.pdfplumber_ocr import PdfplumberProvider

        return await PdfplumberProvider().extract_page(source_path, page_num)

    # ── Scan path: optionally preprocess, then PaddleOCR ─────────────────────
    # Keep the deskewed-only image separate: VLM receives it instead of the
    # binarized version so handwriting recognition is not degraded (M-4).
    from app.ingest.ocr.paddle_ocr import PaddleProvider

    preprocess_now = page_type in (
        PageType.BLURRY_SCAN,
        PageType.HANDWRITING_LIKELY,
        PageType.DEGRADED_SCAN,
    )
    deskewed_image = page_image
    paddle_image = page_image
    if preprocess_now:
        deskewed_image, paddle_image = _build_preprocessed_images(page_image, cfg)

    extraction = await PaddleProvider().extract_page(
        source_path, page_num, page_image=paddle_image
    )

    # ── CLEAN_SCAN retry: degraded vintage prints that the classifier missed.
    # If PaddleOCR underperformed on a page we thought was clean, retry once
    # with full preprocessing before spending VLM budget.
    if (
        page_type == PageType.CLEAN_SCAN
        and extraction.mean_confidence < cfg.paddleocr_confidence_threshold
    ):
        deskewed_image, paddle_image = _build_preprocessed_images(page_image, cfg)
        retry_extraction = await PaddleProvider().extract_page(
            source_path, page_num, page_image=paddle_image
        )
        if retry_extraction.mean_confidence > extraction.mean_confidence:
            logger.debug(
                "ocr_clean_scan_retry_improved",
                doc_id=doc_id,
                page=page_num,
                before=round(extraction.mean_confidence, 3),
                after=round(retry_extraction.mean_confidence, 3),
            )
            extraction = retry_extraction

    # ── VLM escalation check ──────────────────────────────────────────────────
    # Gate on confidence only: a low-quality page can need VLM regardless of
    # whether the classifier tagged it as blurry/handwriting/degraded. The
    # per-document cap (NN-6) still bounds total spend.
    needs_vlm = (
        extraction.mean_confidence < cfg.paddleocr_confidence_threshold
        and llm_router is not None
    )
    if not needs_vlm:
        return extraction

    # Atomic increment: fails silently when budget is exhausted (NN-6)
    claimed = await _try_claim_vlm_page(doc_id, session, cfg.max_vlm_pages_per_doc)
    if not claimed:
        logger.info(
            "vlm_budget_exceeded",
            doc_id=doc_id,
            page=page_num,
            cap=cfg.max_vlm_pages_per_doc,
        )
        extraction.source = "vlm_budget_exceeded"
        return extraction

    from app.ingest.ocr.vlm_ocr import VlmProvider

    try:
        # Pass the deskewed-only (colour) image to the VLM, not the binarized one
        vlm_extraction = await VlmProvider(llm_router).extract_page(
            source_path, page_num, page_image=deskewed_image
        )
        return vlm_extraction
    except BudgetExceededError:
        # Global hourly cap hit — roll back the per-doc slot so it isn't wasted
        await _unclaim_vlm_page(doc_id, session)
        logger.info(
            "vlm_global_budget_exceeded_fallback",
            doc_id=doc_id,
            page=page_num,
        )
        extraction.source = "vlm_budget_exceeded"
        return extraction
    except Exception as exc:
        logger.warning(
            "vlm_ocr_failed_fallback_to_paddle",
            doc_id=doc_id,
            page=page_num,
            error=str(exc),
        )
        return extraction


# ── Preprocessing helper ──────────────────────────────────────────────────────

def _build_preprocessed_images(
    page_image: np.ndarray, cfg: OCRConfig
) -> tuple[np.ndarray, np.ndarray]:
    """Return (deskewed_only, fully_preprocessed) images for one page.

    The deskewed-only colour image is what we hand to the VLM; the fully
    preprocessed (deskew + denoise + binarize) image is what we feed PaddleOCR.
    """
    from app.ingest.ocr.base import OCRConfig as _OCRConfig
    from app.ingest.ocr.base import PreprocessConfig
    from app.ingest.ocr.preprocess import preprocess_image

    deskew_only_cfg = _OCRConfig(
        preprocess=PreprocessConfig(
            deskew_max_angle_deg=cfg.preprocess.deskew_max_angle_deg,
            denoise=False,
            binarize=False,
        )
    )
    deskewed = preprocess_image(page_image, config=deskew_only_cfg)
    full = preprocess_image(page_image, config=cfg)
    return deskewed, full


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _try_claim_vlm_page(doc_id: str, session: AsyncSession, cap: int) -> bool:
    """Atomically increment vlm_pages_used if under cap and commit.

    The commit makes the increment visible to concurrent workers before the
    VLM call fires, preventing double-spending under pgbouncer transaction-
    pooling mode (B-2, NN-6).  A rollback on DB error resets the session so
    subsequent pages can still be processed (E-2).
    """
    try:
        result = await session.execute(
            text(
                """
                UPDATE app.documents
                   SET vlm_pages_used = vlm_pages_used + 1
                 WHERE id = :id
                   AND vlm_pages_used < :cap
                RETURNING vlm_pages_used
                """
            ),
            {"id": doc_id, "cap": cap},
        )
        claimed = result.rowcount > 0
        await session.commit()
        return claimed
    except Exception:
        await session.rollback()
        raise


async def _unclaim_vlm_page(doc_id: str, session: AsyncSession) -> None:
    """Roll back a claimed VLM page slot when the global budget was hit."""
    try:
        await session.execute(
            text(
                "UPDATE app.documents SET vlm_pages_used = GREATEST(0, vlm_pages_used - 1) WHERE id = :id"
            ),
            {"id": doc_id},
        )
        await session.commit()
    except Exception:
        await session.rollback()
