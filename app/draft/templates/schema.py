from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, field_validator


class FieldSpec(BaseModel):
    name: str
    type: Literal["string", "date", "money", "list[string]", "list[party]"]
    description: str
    retrieval_key: str
    required: bool = True


class SectionSpec(BaseModel):
    name: str
    description: str
    retrieval_key: str
    target_length_min: int
    target_length_max: int
    validators: list[str] = []


class ValidatorSpec(BaseModel):
    id: str
    args: dict[str, Any] = {}


class FewShotExample(BaseModel):
    input_summary: str
    output_text: str


class DraftTemplate(BaseModel):
    id: str
    display_name: str
    description: str = ""
    system_prompt: str
    appended_rules: list[str] = []
    retrieval_queries: dict[str, str]
    extraction_schema: list[FieldSpec] = []
    sections: list[SectionSpec]
    validators: list[ValidatorSpec] = []
    version: int = 0

    def compute_fingerprint(self) -> str:
        canonical_dict = {
            "system_prompt": unicodedata.normalize("NFC", self.system_prompt),
            "appended_rules": [
                unicodedata.normalize("NFC", r) for r in self.appended_rules
            ],
            "extraction_schema": {
                f.name: f.model_dump(mode="json") for f in self.extraction_schema
            },
            "sections": [s.model_dump(mode="json") for s in self.sections],
            "retrieval_queries": dict(sorted(self.retrieval_queries.items())),
        }
        canonical = json.dumps(
            canonical_dict,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
