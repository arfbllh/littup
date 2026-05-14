"""Unit tests for extract_entities regex patterns — no DB, no async."""

from __future__ import annotations

import pytest

from app.ingest.chunker import extract_entities


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has(text: str, substring: str) -> bool:
    """Return True if any extracted entity contains the given substring."""
    return any(substring in e for e in extract_entities(text))


def _exact(text: str, entity: str) -> bool:
    """Return True if the exact entity string appears in the extracted list."""
    return entity in extract_entities(text)


# ── Statute citations ─────────────────────────────────────────────────────────

class TestStatuteCitations:
    """Pattern: r'\\b\\d+\\s+U\\.?S\\.?C\\.?\\s*§?\\s*\\d+(?:\\([a-z]\\))?\\b'"""

    def test_positive_dotted_usc_with_section_symbol(self):
        text = "The claim arises under 42 U.S.C. § 1983."
        assert _has(text, "1983"), f"Expected statute cite in: {extract_entities(text)}"

    def test_positive_no_dots_usc(self):
        text = "Charged under 18 USC 1343 for wire fraud."
        assert _has(text, "1343"), f"Expected statute cite in: {extract_entities(text)}"

    def test_positive_usc_with_subsection(self):
        text = "See 28 U.S.C. § 1331(a) for federal question jurisdiction."
        entities = extract_entities(text)
        assert any("1331" in e for e in entities), f"Got: {entities}"

    def test_negative_plain_number_not_statute(self):
        text = "The report contains 42 items and 1983 entries."
        entities = extract_entities(text)
        # Should not match bare numbers without USC
        assert not any(e.strip() == "42" for e in entities)
        assert not any(e.strip() == "1983" for e in entities)

    def test_negative_partial_usc_no_number(self):
        """'U.S.C.' alone without surrounding digits should not match."""
        text = "The United States Code, abbreviated U.S.C., governs federal law."
        entities = extract_entities(text)
        # No full statute pattern matched (no leading digit section number)
        statute_entities = [e for e in entities if "U.S.C" in e]
        assert len(statute_entities) == 0, f"Unexpected: {statute_entities}"


# ── Case citations ────────────────────────────────────────────────────────────

class TestCaseCitations:
    """Pattern: r'\\b[A-Z]\\w+\\s+v\\.\\s+[A-Z]\\w+'"""

    def test_positive_landmark_case(self):
        text = "As held in Brown v. Board, segregation is unconstitutional."
        assert _exact(text, "Brown v. Board"), f"Got: {extract_entities(text)}"

    def test_positive_two_word_parties(self):
        text = "Miranda v. Arizona established the right to counsel."
        assert _exact(text, "Miranda v. Arizona"), f"Got: {extract_entities(text)}"

    def test_positive_corporate_name(self):
        text = "See Zubulake v. UBS for e-discovery standards."
        assert _has(text, "Zubulake v. UBS"), f"Got: {extract_entities(text)}"

    def test_negative_lowercase_v(self):
        """'plaintiff vs. defendant' (lowercase) should not be a case cite."""
        text = "The conflict is plaintiff versus defendant."
        entities = extract_entities(text)
        assert not any(" v. " in e for e in entities)

    def test_negative_abbreviation_v_in_sentence(self):
        """'e.g. V. ' (abbreviation dot) should not trigger."""
        text = "The study evaluated V. cholerae strains."
        # Should not match unless it has the pattern Capital v. Capital
        entities = extract_entities(text)
        case_cites = [e for e in entities if " v. " in e]
        assert len(case_cites) == 0, f"Unexpected case cite: {case_cites}"


# ── Proper noun runs ──────────────────────────────────────────────────────────

class TestProperNouns:
    """Pattern: r'(?<!\\.\s)\\b(?:[A-Z][a-z]+\\s){1,3}[A-Z][a-z]+\\b'"""

    def test_positive_two_word_name(self):
        text = "Harvey Specter filed the motion."
        assert _has(text, "Harvey Specter"), f"Got: {extract_entities(text)}"

    def test_positive_three_word_firm_name(self):
        text = "The firm Pearson Specter Litt is well known."
        assert _has(text, "Pearson Specter Litt"), f"Got: {extract_entities(text)}"

    def test_positive_person_with_middle_name(self):
        text = "Donna Roberta Paulsen managed the records."
        assert _has(text, "Donna Roberta Paulsen"), f"Got: {extract_entities(text)}"

    def test_negative_all_uppercase_acronym(self):
        """All-caps acronyms like 'FBI' should not match [A-Z][a-z]+ pattern."""
        text = "The FBI investigated the case."
        entities = extract_entities(text)
        # FBI is [A-Z][A-Z]+ — won't match [A-Z][a-z]+
        assert "FBI" not in entities

    def test_negative_single_capital_word(self):
        """A single capitalized word with no following capitalized word."""
        text = "The contract was signed."
        entities = extract_entities(text)
        # "The" alone shouldn't appear
        assert "The" not in entities


# ── Dollar amounts ────────────────────────────────────────────────────────────

class TestMoneyAmounts:
    """Pattern: r'\\$[\\d,]+(?:\\.\\d{2})?'"""

    def test_positive_large_amount_with_commas(self):
        text = "The settlement totaled $1,500,000."
        assert _exact(text, "$1,500,000"), f"Got: {extract_entities(text)}"

    def test_positive_small_amount_with_cents(self):
        text = "The filing fee is $75.00."
        assert _exact(text, "$75.00"), f"Got: {extract_entities(text)}"

    def test_positive_whole_dollar_no_cents(self):
        text = "She paid $500 for the retainer."
        assert _exact(text, "$500"), f"Got: {extract_entities(text)}"

    def test_negative_bare_number_no_dollar_sign(self):
        text = "The amount is 1500000 dollars."
        entities = extract_entities(text)
        assert not any("1500000" in e and e.startswith("$") for e in entities)

    def test_negative_euro_sign(self):
        """Euro amounts do not match the dollar pattern."""
        text = "Costs were €500."
        entities = extract_entities(text)
        assert not any(e.startswith("$") for e in entities)


# ── Full dates ────────────────────────────────────────────────────────────────

class TestDates:
    """Pattern: r'\\b(?:January|...|December)\\s+\\d{1,2},?\\s+\\d{4}\\b'"""

    def test_positive_date_with_comma(self):
        text = "The agreement was signed on January 15, 2026."
        assert _exact(text, "January 15, 2026"), f"Got: {extract_entities(text)}"

    def test_positive_date_without_comma(self):
        text = "Filed on March 3 2025."
        assert _exact(text, "March 3 2025"), f"Got: {extract_entities(text)}"

    def test_positive_december_date(self):
        text = "Effective December 31, 2024."
        assert _exact(text, "December 31, 2024"), f"Got: {extract_entities(text)}"

    def test_negative_abbreviated_month(self):
        """Abbreviated months like 'Jan.' should not match the full-name pattern."""
        text = "Signed on Jan. 15, 2026."
        entities = extract_entities(text)
        assert not any("Jan." in e for e in entities)

    def test_negative_numeric_date(self):
        """Pure numeric dates like '01/15/2026' should not match."""
        text = "The date was 01/15/2026."
        entities = extract_entities(text)
        date_entities = [e for e in entities if "2026" in e]
        assert len(date_entities) == 0, f"Unexpected: {date_entities}"


# ── deduplication ─────────────────────────────────────────────────────────────

class TestDeduplication:
    def test_duplicate_entity_appears_once(self):
        text = "Brown v. Board is cited. Brown v. Board is also referenced again."
        entities = extract_entities(text)
        count = sum(1 for e in entities if e == "Brown v. Board")
        assert count == 1, f"Expected 1 occurrence, got {count}: {entities}"

    def test_multiple_entity_types_all_returned(self):
        text = (
            "Harvey Specter filed under 42 U.S.C. § 1983 on January 15, 2026 "
            "for $500,000 in Brown v. Board."
        )
        entities = extract_entities(text)
        assert len(entities) >= 4, f"Expected at least 4 entity types, got: {entities}"

    def test_empty_text_returns_empty(self):
        assert extract_entities("") == []

    def test_no_entities_returns_empty(self):
        assert extract_entities("the quick brown fox jumps over the lazy dog") == []
