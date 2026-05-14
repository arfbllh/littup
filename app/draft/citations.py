"""Pure citation parsing helpers used by generator and (later) M8 validator."""
from __future__ import annotations
import re
from typing import NamedTuple

_CHUNK_REF_RE = re.compile(
    r"\[chunk:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]"
)


class CitationSpan(NamedTuple):
    chunk_id: str
    start: int
    end: int


def parse_citations(text: str) -> list[CitationSpan]:
    """Extract all valid [chunk:UUID] references with their spans."""
    return [CitationSpan(m.group(1), m.start(), m.end()) for m in _CHUNK_REF_RE.finditer(text)]


def strip_dangling(text: str, allowlist: set[str]) -> tuple[str, list[str]]:
    """
    Remove [chunk:UUID] references whose chunk_id is not in allowlist.
    Returns (cleaned_text, list_of_dangling_chunk_ids).
    """
    dangling: list[str] = []
    offset = 0
    result = list(text)

    for m in _CHUNK_REF_RE.finditer(text):
        chunk_id = m.group(1)
        if chunk_id not in allowlist:
            dangling.append(chunk_id)

    if not dangling:
        return text, []

    dangling_set = set(dangling)

    def replace_dangling(m: re.Match) -> str:
        if m.group(1) in dangling_set:
            return ""
        return m.group(0)

    cleaned = _CHUNK_REF_RE.sub(replace_dangling, text)
    return cleaned, dangling
