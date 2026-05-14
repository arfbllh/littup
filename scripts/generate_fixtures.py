"""Generate OCR test fixtures.

Run once before integration tests:
    python scripts/generate_fixtures.py

Requires: reportlab (dev dep), Pillow
Generates deterministic PDFs in tests/fixtures/docs/.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

FIXTURE_DIR = Path("tests/fixtures/docs")


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    _gen_native_clean()
    _gen_scan_clean()
    _gen_scan_skewed()
    _gen_multi_column()
    _gen_corrupt()
    print("Fixtures written to", FIXTURE_DIR)


# ── Native PDF with text layer ────────────────────────────────────────────────

def _gen_native_clean() -> None:
    dest = FIXTURE_DIR / "native_clean.pdf"
    if dest.exists():
        return
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(dest), pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, h - 72, "IN THE UNITED STATES DISTRICT COURT")
    c.setFont("Helvetica", 12)
    lines = [
        "FOR THE SOUTHERN DISTRICT OF NEW YORK",
        "",
        "Pearson Specter Litt LLP, Plaintiff,",
        "   v.                                        Case No. 24-CV-00042",
        "Acme Corp., Defendant.",
        "",
        "COMPLAINT FOR BREACH OF CONTRACT",
        "",
        "Plaintiff Pearson Specter Litt LLP ('PSL'), by and through its",
        "undersigned counsel, hereby alleges as follows:",
        "",
        "1. This is an action for breach of contract arising out of Defendant",
        "   Acme Corp.'s failure to pay legal fees owed to Plaintiff.",
        "",
        "2. Plaintiff PSL is a limited liability partnership organised and",
        "   existing under the laws of the State of New York.",
        "",
        "3. Defendant Acme Corp. is a Delaware corporation with its principal",
        "   place of business at 1 Acme Plaza, New York, NY 10001.",
        "",
        "WHEREFORE, Plaintiff demands judgment against Defendant.",
        "",
        "Dated: January 15, 2024",
        "Harvey Specter, Esq.",
    ]
    y = h - 110
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    c.save()
    print(f"  wrote {dest.name}")


# ── Scan (rasterise the native PDF at 200 dpi) ────────────────────────────────

def _gen_scan_clean() -> None:
    dest = FIXTURE_DIR / "scan_clean.pdf"
    if dest.exists():
        return
    src = FIXTURE_DIR / "native_clean.pdf"
    if not src.exists():
        _gen_native_clean()
    _rasterise_and_repdf(src, dest, dpi=200, rotate=0)
    print(f"  wrote {dest.name}")


# ── Skewed scan (rotate 3°) ───────────────────────────────────────────────────

def _gen_scan_skewed() -> None:
    dest = FIXTURE_DIR / "scan_skewed.pdf"
    if dest.exists():
        return
    src = FIXTURE_DIR / "native_clean.pdf"
    if not src.exists():
        _gen_native_clean()
    _rasterise_and_repdf(src, dest, dpi=200, rotate=3)
    print(f"  wrote {dest.name}")


# ── Two-column layout ─────────────────────────────────────────────────────────

def _gen_multi_column() -> None:
    dest = FIXTURE_DIR / "multi_column.pdf"
    if dest.exists():
        return
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(dest), pagesize=letter)
    w, h = letter
    col_w = (w - 3 * 36) / 2  # two cols with margins

    def draw_col(x: float, header: str, lines: list[str]) -> None:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, h - 72, header)
        c.setFont("Helvetica", 9)
        y = h - 92
        for ln in lines:
            c.drawString(x, y, ln)
            y -= 14

    draw_col(
        36,
        "SECTION I — FACTS",
        [
            "On or about March 1 2024,",
            "Defendant entered into a",
            "retainer agreement with",
            "Plaintiff for legal services.",
            "The agreed fee was $500,000.",
            "Defendant paid $100,000 and",
            "refused to pay the balance.",
        ],
    )
    draw_col(
        36 + col_w + 36,
        "SECTION II — DAMAGES",
        [
            "Plaintiff suffered damages of",
            "$400,000 as a direct result",
            "of Defendant's breach.",
            "Plaintiff also incurred",
            "consequential damages and",
            "attorneys' fees in pursuing",
            "this action.",
        ],
    )
    c.save()
    print(f"  wrote {dest.name}")


# ── Corrupt file ──────────────────────────────────────────────────────────────

def _gen_corrupt() -> None:
    dest = FIXTURE_DIR / "corrupt.pdf"
    if dest.exists():
        return
    dest.write_bytes(b"%PDF-1.4\n" + b"\x00" * 50 + b"GARBAGE_BYTES_NOT_A_VALID_PDF")
    print(f"  wrote {dest.name}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rasterise_and_repdf(src: Path, dest: Path, *, dpi: int, rotate: float) -> None:
    """Rasterise src PDF at `dpi`, optionally rotate, then wrap in a new PDF."""
    from pdf2image import convert_from_path
    from PIL import Image
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as rl_canvas

    pages = convert_from_path(str(src), dpi=dpi)
    c = rl_canvas.Canvas(str(dest))
    for pil_img in pages:
        if rotate:
            pil_img = pil_img.rotate(-rotate, expand=True, fillcolor="white")
        pw, ph = pil_img.size
        # Set page size to match image dimensions (in points at 72 dpi)
        c.setPageSize((pw * 72 / dpi, ph * 72 / dpi))
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            pil_img.save(tmp_path, "PNG")
            c.drawImage(tmp_path, 0, 0, width=pw * 72 / dpi, height=ph * 72 / dpi)
        finally:
            os.unlink(tmp_path)
        c.showPage()
    c.save()


if __name__ == "__main__":
    main()
