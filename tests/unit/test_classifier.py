"""Unit tests for the page classifier (no OCR model, no DB)."""

from __future__ import annotations

import numpy as np
import pytest

from app.ingest.classifier import PageType, classify_page
from app.ingest.ocr.base import ClassifyConfig, OCRConfig, reset_ocr_config_cache

# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_config_cache():
    reset_ocr_config_cache()
    yield
    reset_ocr_config_cache()


# ── Helper ───────────────────────────────────────────────────────────────────

def cfg() -> OCRConfig:
    from app.ingest.ocr.base import load_ocr_config

    return load_ocr_config()


# ── Image factories ──────────────────────────────────────────────────────────

def _clean_scan() -> np.ndarray:
    """White background with sharp horizontal lines — CLEAN_SCAN."""
    import cv2

    img = np.ones((600, 400, 3), dtype=np.uint8) * 255
    for y in range(60, 560, 30):
        cv2.line(img, (20, y), (380, y), (0, 0, 0), 2)
    return img


def _blurry() -> np.ndarray:
    """Heavily blurred — BLURRY_SCAN."""
    import cv2

    return cv2.GaussianBlur(_clean_scan(), (51, 51), 0)


def _handwriting() -> np.ndarray:
    """Lots of diagonal strokes — HANDWRITING_LIKELY."""
    import cv2

    img = np.ones((600, 400, 3), dtype=np.uint8) * 255
    for i in range(12):
        x_off = i * 35
        cv2.line(img, (x_off, 20), (x_off + 280, 560), (0, 0, 0), 2)
        cv2.line(img, (x_off + 280, 20), (x_off, 560), (0, 0, 0), 2)
    return img


def _degraded_print() -> np.ndarray:
    """Sharp horizontal text strokes peppered with salt-and-pepper noise —
    DEGRADED_SCAN (sharp enough to clear the blur threshold, no diagonals,
    but plenty of speckle).
    """
    img = _clean_scan()
    rng = np.random.default_rng(seed=42)
    # Salt-and-pepper density ~6% — typical of vintage prints / photocopies
    h, w = img.shape[:2]
    mask = rng.random((h, w))
    img[mask < 0.03] = 0      # pepper
    img[mask > 0.97] = 255    # salt (already white, but cements the mask shape)
    return img


# ── Tests ────────────────────────────────────────────────────────────────────

def test_native_flag_overrides_image():
    """has_text_layer=True always yields NATIVE regardless of pixel content."""
    img = _blurry()  # would be BLURRY_SCAN on its own
    page_type, conf = classify_page(img, has_text_layer=True)
    assert page_type == PageType.NATIVE
    assert conf == 1.0


def test_clean_scan_is_default():
    img = _clean_scan()
    page_type, conf = classify_page(img)
    assert page_type == PageType.CLEAN_SCAN
    assert 0.5 <= conf <= 1.0


def test_blurry_detected():
    img = _blurry()
    page_type, conf = classify_page(img)
    assert page_type == PageType.BLURRY_SCAN
    assert conf > 0.3


def test_handwriting_detected():
    img = _handwriting()
    page_type, conf = classify_page(img)
    assert page_type == PageType.HANDWRITING_LIKELY
    assert conf > cfg().classify.handwriting_stroke_ratio_threshold


def test_blurry_threshold_configurable():
    """Raising the threshold to a very high value should catch the clean image."""
    high_threshold_cfg = OCRConfig(
        classify=ClassifyConfig(blurry_laplacian_variance_threshold=10_000)
    )
    img = _clean_scan()
    page_type, _ = classify_page(img, config=high_threshold_cfg)
    assert page_type == PageType.BLURRY_SCAN


def test_result_confidence_in_range():
    for img in [_clean_scan(), _blurry(), _handwriting(), _degraded_print()]:
        _, conf = classify_page(img)
        assert 0.0 <= conf <= 1.0


def test_degraded_scan_detected():
    """A sharp page with salt-and-pepper noise should land as DEGRADED_SCAN —
    not BLURRY (Laplacian variance stays high with noise) and not CLEAN
    (speckle ratio exceeds the threshold).
    """
    img = _degraded_print()
    page_type, conf = classify_page(img)
    assert page_type == PageType.DEGRADED_SCAN
    assert conf > cfg().classify.noise_speckle_ratio_threshold


def test_degraded_threshold_configurable():
    """Lifting the speckle threshold to 1.0 should suppress DEGRADED detection
    entirely (no real-world page has noise_ratio > 1).
    """
    suppressed_cfg = OCRConfig(
        classify=ClassifyConfig(noise_speckle_ratio_threshold=1.0)
    )
    img = _degraded_print()
    page_type, _ = classify_page(img, config=suppressed_cfg)
    assert page_type == PageType.CLEAN_SCAN
