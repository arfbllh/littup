"""Per-draft fingerprint must fold in extra_instructions.

Two drafts with the same template but different operator prompts must NOT
collide in the LLM cache or the edit log.
"""

from __future__ import annotations

from app.draft.engine import _compose_fingerprint


def test_extra_instructions_changes_fingerprint() -> None:
    template_fp = "a" * 64
    fp_a = _compose_fingerprint(template_fp, "Focus on indemnification.")
    fp_b = _compose_fingerprint(template_fp, "Ignore Schedule B.")
    assert fp_a != fp_b
    assert fp_a != template_fp
    assert fp_b != template_fp
    assert len(fp_a) == 64


def test_no_extra_instructions_preserves_template_fingerprint() -> None:
    template_fp = "b" * 64
    assert _compose_fingerprint(template_fp, None) == template_fp
    assert _compose_fingerprint(template_fp, "") == template_fp


def test_compose_is_deterministic_and_normalizes_unicode() -> None:
    template_fp = "c" * 64
    # NFC vs NFD of the same accented string — should hash identically after NFC.
    nfc = "café"
    nfd = "café"
    assert nfc != nfd
    assert _compose_fingerprint(template_fp, nfc) == _compose_fingerprint(template_fp, nfd)
