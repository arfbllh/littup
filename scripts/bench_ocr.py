"""make bench-ocr — report mean confidence per fixture document.

Run after: python scripts/generate_fixtures.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURE_DIR = Path("tests/fixtures/docs")
FIXTURES = [
    ("native_clean.pdf", "native"),
    ("scan_clean.pdf", "scan"),
    ("scan_skewed.pdf", "skewed"),
    ("multi_column.pdf", "multi_column"),
]


async def bench_one(name: str, label: str) -> None:
    import cv2
    import numpy as np
    from pdf2image import convert_from_path

    path = FIXTURE_DIR / name
    if not path.exists():
        print(f"  {label:<20} MISSING — run `make fixtures` first")
        return

    from app.ingest.ocr.base import load_ocr_config
    from app.ingest.ocr.pdfplumber_ocr import has_text_layer
    from app.ingest.ocr.routing import route_and_extract

    cfg = load_ocr_config()
    native = has_text_layer(path)
    pages = convert_from_path(str(path), dpi=300)
    images = [cv2.cvtColor(np.array(p), cv2.COLOR_RGB2BGR) for p in pages]

    confidences: list[float] = []
    for i, img in enumerate(images, start=1):
        # Bench uses a fake session/doc_id; VLM path won't be hit (no router)
        ext = await route_and_extract(
            page_image=img,
            source_path=path,
            page_num=i,
            has_text_layer=native,
            doc_id="bench",
            session=None,  # type: ignore[arg-type]
            config=cfg,
            llm_router=None,
        )
        confidences.append(ext.mean_confidence)

    mean = sum(confidences) / len(confidences) if confidences else 0.0
    per_page = "  ".join(f"p{i+1}={c:.2f}" for i, c in enumerate(confidences))
    print(f"  {label:<20} mean={mean:.3f}  [{per_page}]")

    from app.ingest.ocr.paddle_ocr import release_paddle
    release_paddle()


async def main() -> None:
    print("OCR bench — mean confidence per fixture\n")
    for name, label in FIXTURES:
        await bench_one(name, label)


if __name__ == "__main__":
    asyncio.run(main())
