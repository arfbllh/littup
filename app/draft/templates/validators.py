from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.draft.templates.schema import DraftTemplate


@dataclass
class ValidatorResult:
    field_or_section: str
    validator_id: str
    status: Literal["ok", "warn", "fail"]
    message: str


class ValidatorRegistry:
    @classmethod
    def non_empty(cls, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str) and value.strip() == "":
            return False
        if isinstance(value, list) and len(value) == 0:
            return False
        return True

    @classmethod
    def is_date(cls, value: str) -> bool:
        try:
            from dateutil import parser as dateutil_parser

            dateutil_parser.parse(value)
            return True
        except (ValueError, OverflowError):
            return False
        except Exception:
            return False

    @classmethod
    def is_money(cls, value: str) -> bool:
        stripped = value.strip()
        pattern = r"^\$?[\d,]+(\.\d{1,2})?$"
        return bool(re.match(pattern, stripped))

    @classmethod
    def length_between(cls, text: str, min_w: int, max_w: int) -> bool:
        words = text.split()
        return min_w <= len(words) <= max_w

    @classmethod
    def cites_at_least_one(cls, citations: list) -> bool:
        return len(citations) > 0


def run(
    template: DraftTemplate,
    fields: dict[str, Any],
    sections: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    field_chunk_ids: dict[str, list[str]] | None = None,
) -> list[ValidatorResult]:
    results: list[ValidatorResult] = []

    reg = ValidatorRegistry

    # Build a quick lookup for section text by name
    section_text_by_name: dict[str, str] = {}
    for sec in sections:
        name = sec.get("name", "")
        text = sec.get("text", "")
        section_text_by_name[name] = text

    # Run template-level validators
    for v in template.validators:
        vid = v.id
        args = v.args

        if vid == "non_empty":
            target_field = args.get("field")
            target_section = args.get("section")

            if target_field is not None:
                value = fields.get(target_field)
                ok = reg.non_empty(value)
                results.append(
                    ValidatorResult(
                        field_or_section=target_field,
                        validator_id=vid,
                        status="ok" if ok else "fail",
                        message="" if ok else f"Field '{target_field}' must not be empty.",
                    )
                )
            elif target_section is not None:
                text = section_text_by_name.get(target_section, "")
                ok = reg.non_empty(text)
                results.append(
                    ValidatorResult(
                        field_or_section=target_section,
                        validator_id=vid,
                        status="ok" if ok else "fail",
                        message="" if ok else f"Section '{target_section}' must not be empty.",
                    )
                )

        elif vid == "is_date":
            target_field = args.get("field")
            if target_field is not None:
                value = fields.get(target_field)
                if value is None:
                    status = "warn"
                    msg = f"Field '{target_field}' is missing, cannot validate as date."
                else:
                    ok = reg.is_date(str(value))
                    status = "ok" if ok else "fail"
                    msg = "" if ok else f"Field '{target_field}' is not a valid date."
                results.append(
                    ValidatorResult(
                        field_or_section=target_field,
                        validator_id=vid,
                        status=status,
                        message=msg,
                    )
                )

        elif vid == "is_money":
            target_field = args.get("field")
            if target_field is not None:
                value = fields.get(target_field)
                if value is None:
                    status = "warn"
                    msg = f"Field '{target_field}' is missing, cannot validate as money."
                else:
                    ok = reg.is_money(str(value))
                    status = "ok" if ok else "fail"
                    msg = "" if ok else f"Field '{target_field}' is not a valid money value."
                results.append(
                    ValidatorResult(
                        field_or_section=target_field,
                        validator_id=vid,
                        status=status,
                        message=msg,
                    )
                )

        elif vid == "length_between":
            target_section = args.get("section")
            min_w = int(args.get("min", 0))
            max_w = int(args.get("max", 99999))
            if target_section is not None:
                text = section_text_by_name.get(target_section, "")
                ok = reg.length_between(text, min_w, max_w)
                results.append(
                    ValidatorResult(
                        field_or_section=target_section,
                        validator_id=vid,
                        status="ok" if ok else "warn",
                        message=(
                            ""
                            if ok
                            else (
                                f"Section '{target_section}' word count is outside "
                                f"[{min_w}, {max_w}]."
                            )
                        ),
                    )
                )

        elif vid == "cites_at_least_one":
            target_section = args.get("section")
            target_field = args.get("field")
            if target_section is not None:
                filtered = [
                    c for c in citations if c.get("section_name") == target_section
                ]
                ok = reg.cites_at_least_one(filtered)
                results.append(
                    ValidatorResult(
                        field_or_section=target_section,
                        validator_id=vid,
                        status="ok" if ok else "warn",
                        message=(
                            ""
                            if ok
                            else f"Section '{target_section}' has no citations."
                        ),
                    )
                )
            elif target_field is not None:
                chunk_ids = (field_chunk_ids or {}).get(target_field, [])
                ok = reg.cites_at_least_one(chunk_ids)
                results.append(
                    ValidatorResult(
                        field_or_section=target_field,
                        validator_id=vid,
                        status="ok" if ok else "warn",
                        message=(
                            ""
                            if ok
                            else f"Field '{target_field}' has no supporting chunks."
                        ),
                    )
                )

    # Run per-section validators listed in SectionSpec.validators
    for sec_spec in template.sections:
        sec_name = sec_spec.name
        text = section_text_by_name.get(sec_name, "")

        for vid in sec_spec.validators:
            if vid == "non_empty":
                ok = reg.non_empty(text)
                results.append(
                    ValidatorResult(
                        field_or_section=sec_name,
                        validator_id=vid,
                        status="ok" if ok else "fail",
                        message="" if ok else f"Section '{sec_name}' must not be empty.",
                    )
                )

            elif vid == "length_between":
                min_w = sec_spec.target_length_min
                max_w = sec_spec.target_length_max
                ok = reg.length_between(text, min_w, max_w)
                results.append(
                    ValidatorResult(
                        field_or_section=sec_name,
                        validator_id=vid,
                        status="ok" if ok else "warn",
                        message=(
                            ""
                            if ok
                            else (
                                f"Section '{sec_name}' word count is outside "
                                f"[{min_w}, {max_w}]."
                            )
                        ),
                    )
                )

            elif vid == "cites_at_least_one":
                filtered = [
                    c for c in citations if c.get("section_name") == sec_name
                ]
                ok = reg.cites_at_least_one(filtered)
                results.append(
                    ValidatorResult(
                        field_or_section=sec_name,
                        validator_id=vid,
                        status="ok" if ok else "warn",
                        message=(
                            ""
                            if ok
                            else f"Section '{sec_name}' has no citations."
                        ),
                    )
                )

    return results
