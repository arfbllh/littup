"""Structured diff computation for field and section edits."""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.draft.templates.schema import DraftTemplate, FieldSpec

_MISSING = object()


@dataclass(frozen=True)
class StructuredDiff:
    fields: dict[str, dict]    # field_name -> diff payload (only changed fields)
    sections: dict[str, dict]  # section_name -> diff payload (only changed sections)

    def is_empty(self) -> bool:
        return not self.fields and not self.sections


def compute_diff(
    template: "DraftTemplate",
    ai_output: dict,   # full draft.ai_output
    user_output: dict, # POST body (partial)
) -> StructuredDiff:
    """Compute a structured diff between AI output and user-submitted output.

    Only produces entries for keys the user actually submitted.
    Missing keys in user_output are treated as unchanged (no diff).
    ai_output keys missing entirely (older drafts) are treated as None baseline.
    """
    ai_fields = ai_output.get("fields") or {}
    user_fields = user_output.get("fields") or {}
    ai_sections_text = ai_output.get("sections_text") or {}
    user_sections_list = user_output.get("sections") or []
    user_sections_by_name = {s["name"]: s["text"] for s in user_sections_list if "name" in s}

    field_diffs: dict[str, dict] = {}
    for field_spec in template.extraction_schema:
        if field_spec.name not in user_fields:
            continue
        ai_entry = ai_fields.get(field_spec.name) or {}
        ai_value = ai_entry.get("value") if ai_entry else None
        user_value = user_fields[field_spec.name]
        diff = compute_field_diff(field_spec, ai_value, user_value)
        if diff is not None:
            field_diffs[field_spec.name] = diff

    section_diffs: dict[str, dict] = {}
    for section_spec in template.sections:
        if section_spec.name not in user_sections_by_name:
            continue
        ai_text = ai_sections_text.get(section_spec.name) or ""
        user_text = user_sections_by_name[section_spec.name] or ""
        diff = compute_section_diff(section_spec.name, ai_text, user_text)
        if diff is not None:
            section_diffs[section_spec.name] = diff

    return StructuredDiff(fields=field_diffs, sections=section_diffs)


def compute_field_diff(
    field_spec: "FieldSpec",
    ai_value: Any,
    user_value: Any,
) -> dict | None:
    """Compute diff for a single field.

    Returns None iff values are deep-equal post-normalization.
    """
    ftype = field_spec.type

    if ai_value is None and user_value is not None:
        return {"ai": ai_value, "user": user_value, "operation": "added"}
    if ai_value is not None and user_value is None:
        return {"ai": ai_value, "user": user_value, "operation": "removed"}

    if ftype in ("string", "date", "money"):
        ai_s = str(ai_value).strip() if ai_value is not None else ""
        user_s = str(user_value).strip() if user_value is not None else ""
        if ai_s == user_s:
            return None
        return {"ai": ai_value, "user": user_value, "operation": "modified"}

    if ftype == "list[string]":
        ai_list = list(ai_value) if ai_value else []
        user_list = list(user_value) if user_value else []
        ai_set = set(ai_list)
        user_set = set(user_list)
        added = sorted(user_set - ai_set)
        removed = sorted(ai_set - user_set)
        if not added and not removed:
            return None
        return {
            "ai": ai_list,
            "user": user_list,
            "operation": "modified",
            "added_items": added,
            "removed_items": removed,
        }

    if ftype == "list[party]":
        ai_list = list(ai_value) if ai_value else []
        user_list = list(user_value) if user_value else []
        ai_by_key = {_party_key(p): p for p in ai_list}
        user_by_key = {_party_key(p): p for p in user_list}
        added_keys = set(user_by_key.keys()) - set(ai_by_key.keys())
        removed_keys = set(ai_by_key.keys()) - set(user_by_key.keys())
        if not added_keys and not removed_keys:
            return None
        return {
            "ai": ai_list,
            "user": user_list,
            "operation": "modified",
            "added_items": [user_by_key[k] for k in sorted(added_keys)],
            "removed_items": [ai_by_key[k] for k in sorted(removed_keys)],
        }

    # Fallback: generic equality
    if ai_value == user_value:
        return None
    return {"ai": ai_value, "user": user_value, "operation": "modified"}


def compute_section_diff(
    section_name: str,
    ai_text: str,
    user_text: str,
) -> dict | None:
    """Compute a token-level diff for a section.

    Both texts are NFC-normalized and stripped before comparison.
    Returns None iff texts are equal post-normalization.
    """
    ai_norm = unicodedata.normalize("NFC", ai_text or "").strip()
    user_norm = unicodedata.normalize("NFC", user_text or "").strip()

    if ai_norm == user_norm:
        return None

    ai_tokens = re.findall(r"\S+|\s+", ai_norm)
    user_tokens = re.findall(r"\S+|\s+", user_norm)

    matcher = difflib.SequenceMatcher(None, ai_tokens, user_tokens, autojunk=False)
    char_changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete", "insert"):
            char_changes.append({
                "op": tag,
                "ai": "".join(ai_tokens[i1:i2]),
                "user": "".join(user_tokens[j1:j2]),
            })

    return {
        "section": section_name,
        "ai_text": ai_norm,
        "user_text": user_norm,
        "char_changes": char_changes,
    }


def _party_key(party: Any) -> str:
    if isinstance(party, dict):
        return party.get("name", "").strip().lower()
    return str(party).strip().lower()
