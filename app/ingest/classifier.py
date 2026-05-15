"""Page-type classifier — pure heuristics, no model weights."""

from __future__ import annotations

from enum import StrEnum

import numpy as np


class PageType(StrEnum):
    NATIVE = "native"             # PDF with embedded text layer
    BLURRY_SCAN = "blurry_scan"   # Low-sharpness scan; deskew + denoise before OCR
    HANDWRITING_LIKELY = "handwriting_likely"  # Dominant diagonal strokes
    DEGRADED_SCAN = "degraded_scan"  # Sharp but noisy: vintage prints, photocopies, faxes
    FORM_LAYOUT = "form_layout"   # Reserved; routed same as CLEAN_SCAN for now
    CLEAN_SCAN = "clean_scan"     # Default; straight to PaddleOCR


def classify_page(
    image: np.ndarray,
    *,
    has_text_layer: bool = False,
    config=None,  # OCRConfig | None — imported lazily to avoid circular deps
) -> tuple[PageType, float]:
    """Return (PageType, classifier_confidence 0–1).

    confidence reflects how certain the heuristic is, not OCR quality.
    When uncertain, defaults to CLEAN_SCAN so the cheap path runs first.
    """
    if has_text_layer:
        return PageType.NATIVE, 1.0

    import cv2

    from app.ingest.ocr.base import load_ocr_config

    cfg = config or load_ocr_config()

    if image.ndim == 2:
        gray = image.copy()
    elif image.shape[2] == 4:
        gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # ── Blur detection via Laplacian variance ────────────────────────────────
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    threshold = cfg.classify.blurry_laplacian_variance_threshold
    if lap_var < threshold:
        conf = float(1.0 - lap_var / max(threshold, 1.0))
        return PageType.BLURRY_SCAN, min(conf, 1.0)

    # ── Handwriting detection via stroke-direction analysis ──────────────────
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=50, minLineLength=30, maxLineGap=10
    )
    if lines is not None and len(lines) >= 5:
        diagonal_count = 0
        for line in lines:
            x1, y1, x2, y2 = line[0]
            dx = abs(x2 - x1)
            dy = abs(y2 - y1)
            if dx + dy == 0:
                continue
            angle = float(np.arctan2(dy, dx))  # in [0, π/2]
            # diagonal = neither near-horizontal (< 22.5°) nor near-vertical (> 67.5°)
            if np.pi / 8 <= angle <= 3 * np.pi / 8:
                diagonal_count += 1
        ratio = diagonal_count / len(lines)
        if ratio > cfg.classify.handwriting_stroke_ratio_threshold:
            return PageType.HANDWRITING_LIKELY, float(ratio)

    # ── Degradation detection via speckle ratio ──────────────────────────────
    # Sharp pages can still be noisy (vintage prints, photocopies, faxes).
    # Count the fraction of connected components in the binarised page that
    # are tiny (<4 px) — typical of salt-and-pepper noise. A clean printed
    # page has speckle ratio ≈ 0 (every component is a large glyph); a
    # degraded scan is dominated by 1–3-pixel specks.
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    num_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if num_labels > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        small_components = int(np.count_nonzero(areas < 4))
        speckle_ratio = small_components / float(num_labels - 1)
        if speckle_ratio > cfg.classify.noise_speckle_ratio_threshold:
            return PageType.DEGRADED_SCAN, min(speckle_ratio, 1.0)

    return PageType.CLEAN_SCAN, 0.9
