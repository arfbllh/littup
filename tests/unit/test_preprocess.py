"""Unit tests for the OpenCV preprocessing pipeline (no OCR model, no DB)."""

from __future__ import annotations

import numpy as np
import pytest


def _horizontal_lines_image(height: int = 600, width: int = 400) -> np.ndarray:
    """Synthetic image: white background with sharp horizontal black lines."""
    import cv2

    img = np.ones((height, width, 3), dtype=np.uint8) * 255
    for y in range(60, height - 40, 40):
        cv2.line(img, (10, y), (width - 10, y), (0, 0, 0), 3)
    return img


def _skew(image: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotate image by angle_deg (positive = counter-clockwise)."""
    import cv2

    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle_deg, 1.0)
    return cv2.warpAffine(image, M, (w, h), borderMode=cv2.BORDER_REPLICATE)


# ── Tests ────────────────────────────────────────────────────────────────────

def _deskew_only_cfg(max_angle: float = 15.0):
    """Config with only deskew enabled — isolates the step under test."""
    from app.ingest.ocr.base import OCRConfig, PreprocessConfig

    return OCRConfig(preprocess=PreprocessConfig(
        deskew_max_angle_deg=max_angle, denoise=False, binarize=False
    ))


def test_deskew_corrects_small_angle():
    """A 5° skew should be corrected to within 0.5° (deskew step in isolation)."""
    from app.ingest.ocr.preprocess import measure_skew_angle, preprocess_image

    original = _horizontal_lines_image()
    skewed = _skew(original, 5.0)

    processed = preprocess_image(skewed, config=_deskew_only_cfg())
    residual = abs(measure_skew_angle(processed))
    assert residual < 0.5, f"Residual skew {residual:.2f}° exceeds 0.5°"


def test_deskew_ignores_large_angle():
    """Angles beyond max_angle_deg are left unchanged (not a skew, probably rotation)."""
    import numpy as np
    from app.ingest.ocr.preprocess import preprocess_image

    cfg = _deskew_only_cfg(max_angle=5.0)
    original = _horizontal_lines_image()
    skewed = _skew(original, 30.0)

    processed = preprocess_image(skewed, config=cfg)
    assert np.array_equal(processed, skewed), (
        "Image with angle > max_angle_deg should be returned unchanged"
    )


def test_preprocess_returns_3channel():
    """Output must always be a 3-channel image (BGR) for downstream compatibility."""
    from app.ingest.ocr.preprocess import preprocess_image

    img = _horizontal_lines_image()
    result = preprocess_image(img)
    assert result.ndim == 3
    assert result.shape[2] == 3


def test_preprocess_output_same_dtype():
    from app.ingest.ocr.preprocess import preprocess_image

    img = _horizontal_lines_image()
    result = preprocess_image(img)
    assert result.dtype == np.uint8


def test_deskew_3deg():
    """The skewed fixture uses 3° — corrected to within 1° (minAreaRect precision at small angles).

    3° is near the lower detection limit for a heuristic estimator, so we allow
    1° residual here instead of the 0.5° required for larger skews.
    """
    from app.ingest.ocr.preprocess import measure_skew_angle, preprocess_image

    # Use a taller image with more lines for better angle signal at small angles
    original = _horizontal_lines_image(height=800, width=600)
    skewed = _skew(original, 3.0)
    processed = preprocess_image(skewed, config=_deskew_only_cfg())
    assert abs(measure_skew_angle(processed)) < 1.0
