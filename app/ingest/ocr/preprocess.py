"""OpenCV preprocessing: deskew, denoise, binarize."""

from __future__ import annotations

import numpy as np


def preprocess_image(
    image: np.ndarray,
    *,
    config=None,  # OCRConfig | None
) -> np.ndarray:
    """Apply deskew → denoise → binarize in order.

    Returns a processed copy; the original is not modified.
    """
    from app.ingest.ocr.base import load_ocr_config

    cfg = config or load_ocr_config()
    out = image.copy()

    out = _deskew(out, cfg.preprocess.deskew_max_angle_deg)
    if cfg.preprocess.denoise:
        out = _denoise(out)
    if cfg.preprocess.binarize:
        out = _binarize(out)

    return out


# ── Internal steps ────────────────────────────────────────────────────────────

def _deskew(image: np.ndarray, max_angle_deg: float) -> np.ndarray:
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image.copy()
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(binary > 0))
    if len(coords) < 10:
        return image

    # cv2.minAreaRect expects (N, 1, 2) float32 with (x, y) = (col, row)
    pts = coords[:, [1, 0]].astype(np.float32)
    angle = cv2.minAreaRect(pts)[-1]

    # minAreaRect returns angles in (-90, 0].
    # For near-horizontal text (longer axis ~horizontal): angle is near 0.
    # The sign convention: a negative angle means the text tilts CW from horizontal.
    # Apply this angle directly as the correction (negative → rotate CW to straighten).
    # For near-vertical text (angle < -45): remap to the complementary angle.
    if angle < -45.0:
        angle = 90.0 + angle

    if abs(angle) > max_angle_deg:
        return image  # Too large to be a skew; leave as-is

    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    return cv2.warpAffine(
        image, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def _denoise(image: np.ndarray) -> np.ndarray:
    import cv2

    if image.ndim == 3:
        return cv2.fastNlMeansDenoisingColored(image, None, h=10, hColor=10, templateWindowSize=7, searchWindowSize=21)
    return cv2.fastNlMeansDenoising(image, None, h=10, templateWindowSize=7, searchWindowSize=21)


def _binarize(image: np.ndarray) -> np.ndarray:
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image.copy()
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, blockSize=11, C=2
    )
    # Return as 3-channel so downstream code doesn't need to handle both shapes
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


def measure_skew_angle(image: np.ndarray) -> float:
    """Return the estimated skew angle in degrees (for testing assertions)."""
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image.copy()
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(binary > 0))
    if len(coords) < 10:
        return 0.0
    pts = coords[:, [1, 0]].astype(np.float32)
    angle = cv2.minAreaRect(pts)[-1]
    # Match _deskew's sign convention exactly: report the rotation that was (or would be) applied.
    if angle < -45.0:
        angle = 90.0 + angle  # match _deskew's convention after C5 fix
    return float(angle)
