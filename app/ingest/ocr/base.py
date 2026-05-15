"""Shared types and config for the OCR pipeline."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import yaml

if TYPE_CHECKING:
    import numpy as np


@dataclass
class OCRSpan:
    text: str
    page: int  # 1-indexed
    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1) normalised 0–1
    confidence: float
    source: Literal["pdfplumber", "paddleocr", "vlm", "docling"]


@dataclass
class PageExtraction:
    page_num: int
    spans: list[OCRSpan]
    full_text: str
    mean_confidence: float
    source: str  # primary source name; "vlm_budget_exceeded" flags a capped page


class OCRProvider(Protocol):
    async def extract_page(
        self,
        source_path: Path,
        page_num: int,
        *,
        page_image: np.ndarray | None = None,
    ) -> PageExtraction: ...


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class ClassifyConfig:
    blurry_laplacian_variance_threshold: float = 100.0
    handwriting_stroke_ratio_threshold: float = 0.35
    noise_speckle_ratio_threshold: float = 0.15


@dataclass
class PreprocessConfig:
    deskew_max_angle_deg: float = 15.0
    denoise: bool = True
    binarize: bool = True


@dataclass
class OCRConfig:
    max_vlm_pages_per_doc: int = 20
    paddleocr_confidence_threshold: float = 0.7
    classify: ClassifyConfig = field(default_factory=ClassifyConfig)
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)


_cached_config: OCRConfig | None = None
_config_lock = threading.Lock()


def load_ocr_config(path: str | None = None) -> OCRConfig:
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    with _config_lock:  # M-5: double-checked locking; safe for threaded executors
        if _cached_config is not None:
            return _cached_config
        from app.settings import settings

        config_path = Path(path or settings.OCR_CONFIG_PATH)
        if not config_path.exists():
            _cached_config = OCRConfig()
            return _cached_config

        raw = yaml.safe_load(config_path.read_text())
        if not isinstance(raw, dict):
            raw = {}
        classify_raw = raw.get("classify", {})
        preprocess_raw = raw.get("preprocess", {})
        _cached_config = OCRConfig(
            max_vlm_pages_per_doc=int(raw.get("max_vlm_pages_per_doc", 20)),
            paddleocr_confidence_threshold=float(raw.get("paddleocr_confidence_threshold", 0.7)),
            classify=ClassifyConfig(
                blurry_laplacian_variance_threshold=float(
                    classify_raw.get("blurry_laplacian_variance_threshold", 100.0)
                ),
                handwriting_stroke_ratio_threshold=float(
                    classify_raw.get("handwriting_stroke_ratio_threshold", 0.35)
                ),
                noise_speckle_ratio_threshold=float(
                    classify_raw.get("noise_speckle_ratio_threshold", 0.15)
                ),
            ),
            preprocess=PreprocessConfig(
                deskew_max_angle_deg=float(preprocess_raw.get("deskew_max_angle_deg", 15.0)),
                denoise=bool(preprocess_raw.get("denoise", True)),
                binarize=bool(preprocess_raw.get("binarize", True)),
            ),
        )
    return _cached_config


def reset_ocr_config_cache() -> None:
    """Clear the module-level config cache; used in tests that override thresholds."""
    global _cached_config
    with _config_lock:
        _cached_config = None
