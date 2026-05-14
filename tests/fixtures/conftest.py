"""Session-scoped fixture that generates minimal synthetic PDFs for integration tests.

PDFs are generated once per pytest session via fpdf2. If fpdf2 is not installed,
tests that depend on the fixtures are skipped.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

DOCS_DIR = Path(__file__).parent / "docs"

_LEGAL_TEXT = (
    "The plaintiff hereby moves this court pursuant to Federal Rule of Civil Procedure 56 "
    "for an order granting summary judgment in its favor. Under 42 U.S.C. § 1983, any "
    "person acting under color of state law who deprives another of rights secured by the "
    "Constitution shall be liable in damages. See Brown v. Board of Education, 347 U.S. 483 "
    "(1954). The amount in controversy exceeds $75,000. This motion was filed on January 15, "
    "2026. Counsel for the plaintiff, Harvey Specter, respectfully submits this brief."
)


def _require_fpdf():
    try:
        import fpdf  # noqa: F401

        return True
    except ImportError:
        return False


def _make_scan_clean() -> None:
    path = DOCS_DIR / "scan_clean.pdf"
    if path.exists():
        return
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Page 1 — introduction
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "MOTION FOR SUMMARY JUDGMENT", ln=True)
    pdf.ln(4)
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 8, _LEGAL_TEXT)

    # Page 2 — continuation
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 10, "ARGUMENT", ln=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(
        0,
        8,
        (
            "I. THE DEFENDANT VIOLATED PLAINTIFF'S CONSTITUTIONAL RIGHTS\n\n"
            "The uncontroverted evidence establishes that the defendant, acting under color "
            "of state law, deprived the plaintiff of his Fourteenth Amendment rights. "
            "The Court of Appeals for the Second Circuit held in Specter v. Litt, 892 F.3d "
            "441 (2d Cir. 2018), that such deprivations give rise to liability under "
            "42 U.S.C. § 1983."
        ),
    )
    pdf.output(str(path))


def _make_multi_column() -> None:
    path = DOCS_DIR / "multi_column.pdf"
    if path.exists():
        return
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "CASE SUMMARY - TWO COLUMN LAYOUT", ln=True)
    pdf.ln(2)

    col_width = 88.0
    col_gap = 14.0
    line_h = 6.0
    start_y = pdf.get_y()

    left_paragraphs = [
        "Column One: Background",
        "On September 3, 2025, the plaintiff initiated this action by filing a complaint "
        "in the United States District Court for the Southern District of New York.",
        "The complaint alleged violations of 18 U.S.C. § 1343 (wire fraud) and "
        "42 U.S.C. § 1983 (civil rights).",
    ]
    right_paragraphs = [
        "Column Two: Relief Sought",
        "The plaintiff seeks compensatory damages of $1,500,000 and punitive damages "
        "of $3,000,000, together with costs and attorneys fees.",
        "Plaintiff also seeks a permanent injunction enjoining defendant from further "
        "violations of the civil rights statutes described herein.",
    ]

    pdf.set_font("Helvetica", size=10)
    y_left = start_y
    for para in left_paragraphs:
        pdf.set_xy(10, y_left)
        pdf.multi_cell(col_width, line_h, para)
        y_left = pdf.get_y() + 3

    y_right = start_y
    for para in right_paragraphs:
        pdf.set_xy(10 + col_width + col_gap, y_right)
        pdf.multi_cell(col_width, line_h, para)
        y_right = pdf.get_y() + 3

    pdf.output(str(path))


def _make_table_heavy() -> None:
    path = DOCS_DIR / "table_heavy.pdf"
    if path.exists():
        return
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "SCHEDULE OF DAMAGES", ln=True)
    pdf.ln(4)

    headers = ["Item", "Description", "Amount ($)"]
    rows = [
        ["1", "Lost wages (Jan-Jun 2025)", "45,000.00"],
        ["2", "Medical expenses", "12,500.00"],
        ["3", "Pain and suffering", "250,000.00"],
        ["4", "Punitive damages", "500,000.00"],
        ["5", "Attorneys fees", "75,000.00"],
        ["6", "Court costs", "3,200.00"],
        ["7", "Expert witness fees", "18,000.00"],
        ["8", "Lost future earnings", "600,000.00"],
    ]

    col_widths = [20.0, 110.0, 50.0]
    row_h = 8.0

    # Header row
    pdf.set_font("Helvetica", "B", 10)
    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], row_h, h, border=1)
    pdf.ln()

    # Data rows
    pdf.set_font("Helvetica", size=10)
    for row in rows:
        for i, cell in enumerate(row):
            pdf.cell(col_widths[i], row_h, cell, border=1)
        pdf.ln()

    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, row_h, "TOTAL: $1,503,700.00", border=0)

    pdf.output(str(path))


def _make_corrupt() -> None:
    path = DOCS_DIR / "corrupt.pdf"
    if path.exists():
        return
    # Write a file that looks like a PDF but has truncated/corrupt content
    path.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n" + b"\x00" * 20)


def _make_title_review_clean() -> None:
    path = DOCS_DIR / "title_review_clean.pdf"
    if path.exists():
        return
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "COMMITMENT FOR TITLE INSURANCE", ln=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", size=11)

    lines = [
        "Commitment No.: TI-2024-00088",
        "Effective Date: March 14, 2024",
        "Property: 42 Maple Street, Albany, NY 12201",
        "",
        "SCHEDULE A",
        "  1. Policy Amount: $400,000",
        "  2. Proposed Insured: Acme Real Estate Holdings, LLC",
        "  3. Estate or Interest: Fee Simple",
        "",
        "SCHEDULE B-I  Requirements",
        "  1. Payment of taxes for year 2023 ($6,450.00)",
        "  2. Execution and recordation of deed from grantor.",
        "  3. Release of mortgage dated Jan 3, 2020 (City Bank).",
        "",
        "SCHEDULE B-II  Exceptions",
        "  1. Rights of parties in possession.",
        "  2. Encumbrances or liens not shown by public records.",
        "  3. Easement along northern boundary — 15 ft. utility corridor.",
    ]
    for line in lines:
        pdf.cell(0, 7, line, ln=True)

    pdf.output(str(path))


def _make_title_review_messy() -> None:
    path = DOCS_DIR / "title_review_messy.pdf"
    if path.exists():
        return
    from fpdf import FPDF

    # Same content as clean but with OCR-noise text (scrambled spacing, mixed case)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Courier", size=10)
    lines = [
        "C0MM1TMENT FOR T1TLE  INSURANCE",
        "Comm itment No.:  TI-2024-00088",
        "Effectlve Date: March 14,  2024",
        "Propert y: 42 Maple  Street,   Albany NY 12201",
        "",
        "SCHED ULE A",
        "  1.  Pol1cy Amount:  $400,000",
        "  2.  Proposed lnsured:  Acme Real Estate  Holdings LLC",
        "  3.  Es tate or Interest:  Fee  Simple",
        "",
        "SCHEDULE  B-I  Requirem ents",
        "  1. Payment of taxes  2023  ($6,450.00)",
        "  2. Deed execution and  recordation.",
        "  3. Release of mortgage  Jan 3, 2020.",
    ]
    for line in lines:
        # Vary x position to simulate messy left margin
        x = 10 + (hash(line) % 4)
        pdf.set_x(x)
        pdf.cell(0, 8, line, ln=True)

    pdf.output(str(path))


def _make_handwriting_excerpt() -> None:
    path = DOCS_DIR / "handwriting_excerpt.pdf"
    if path.exists():
        return
    from fpdf import FPDF

    # Simulate handwritten note using Courier with irregular spacing
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Courier", size=12)

    lines = [
        "Dear  Harvey,",
        "",
        "  I am writing to confirm our agreement dated",
        "  September 3, 2025 regarding the Acme matter.",
        "  The settlement amount of $75,000 was accepted",
        "  by all parties on January 15, 2026.",
        "",
        "  Please file the necessary stipulation with",
        "  the Southern District of New York.",
        "",
        "Sincerely,",
        "  Louis Litt",
    ]
    for i, line in enumerate(lines):
        # Irregular left margin simulates handwriting drift
        x = 15 + (i % 3) * 2
        pdf.set_x(x)
        pdf.cell(0, 9, line, ln=True)

    pdf.output(str(path))


@pytest.fixture(scope="session", autouse=True)
def generate_fixture_pdfs():
    """Generate synthetic fixture PDFs if not already present. Skips if fpdf2 missing."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    _make_corrupt()  # corrupt file is pure bytes, no fpdf2 needed

    if not _require_fpdf():
        pytest.skip("fpdf2 not installed — skipping fixture PDF generation")
        return

    _make_scan_clean()
    _make_multi_column()
    _make_table_heavy()
    _make_title_review_clean()
    _make_title_review_messy()
    _make_handwriting_excerpt()
