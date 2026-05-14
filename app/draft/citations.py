"""Pure citation parsing helpers used by generator and M8 validator."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import NamedTuple

_CHUNK_REF_RE = re.compile(
    r"\[chunk:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]"
)

# Simple sentence boundary: one or more .!? followed by whitespace.
# Does not handle abbreviations like Inc. or U.S.C. — documented limit.
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')
_ONLY_PUNCT_RE = re.compile(r'^[\s\.,;:!?]+$')


class CitationSpan(NamedTuple):
    chunk_id: str
    start: int
    end: int


@dataclass
class Claim:
    text: str
    cited_chunk_ids: list[str]
    char_span: tuple[int, int]


def parse_citations(text: str) -> list[CitationSpan]:
    """Extract all valid [chunk:UUID] references with their spans."""
    return [CitationSpan(m.group(1), m.start(), m.end()) for m in _CHUNK_REF_RE.finditer(text)]


def segment_claims(section_text: str, citations: list) -> list[Claim]:
    """Split section_text into claims based on citation tag positions.

    A claim spans from the previous boundary (sentence start or end of the
    previous citation tag) up to and including the next [chunk:UUID] tag.
    Sentences with no citation tag also become claims (cited_chunk_ids=[]).

    ``citations`` is a list of CitationDraft or CitationSpan objects; any
    object with a ``chunk_id`` attribute is accepted.
    """
    if not section_text:
        return []

    # Collect citation positions sorted by start offset in the text
    ref_matches = list(_CHUNK_REF_RE.finditer(section_text))
    if not ref_matches:
        # No citations at all — whole text is one uncited claim
        return [Claim(text=section_text.strip(), cited_chunk_ids=[], char_span=(0, len(section_text)))]

    claims: list[Claim] = []
    cursor = 0

    for match in ref_matches:
        tag_end = match.end()
        chunk_id = match.group(1)

        # The claim text spans from cursor up to the end of this citation tag.
        # Within that span, walk backwards from match.start() to find the
        # start of the sentence that contains the citation.
        segment = section_text[cursor:tag_end]

        # Find where the sentence containing this citation starts inside
        # the segment.  Split by sentence boundaries; the last fragment
        # before the citation is the current claim's prose.
        parts = _SENTENCE_SPLIT_RE.split(segment)
        claim_prose = parts[-1] if parts else segment

        # Strip the citation tag from the claim text before LLM validation so
        # the UUID token doesn't contaminate faithfulness scoring (NN-7 cache key).
        clean_prose = _CHUNK_REF_RE.sub("", claim_prose).strip()
        claims.append(Claim(
            text=clean_prose,
            cited_chunk_ids=[chunk_id],
            char_span=(cursor, tag_end),
        ))
        cursor = tag_end

    # Any trailing text after the last citation may be uncited claims.
    # Filter out text that is only punctuation/whitespace (e.g., a trailing period).
    trailing = section_text[cursor:].strip()
    if trailing and not _ONLY_PUNCT_RE.match(trailing):
        # Try to split into individual sentences
        sentences = _SENTENCE_SPLIT_RE.split(trailing)
        offset = cursor
        for sent in sentences:
            sent_stripped = sent.strip()
            if sent_stripped and not _ONLY_PUNCT_RE.match(sent_stripped):
                sent_start = section_text.find(sent_stripped, offset)
                sent_end = sent_start + len(sent_stripped) if sent_start != -1 else len(section_text)
                claims.append(Claim(
                    text=sent_stripped,
                    cited_chunk_ids=[],
                    char_span=(sent_start if sent_start != -1 else offset, sent_end),
                ))
                offset = sent_end

    return claims


def strip_dangling(text: str, allowlist: set[str]) -> tuple[str, list[str]]:
    """
    Remove [chunk:UUID] references whose chunk_id is not in allowlist.
    Returns (cleaned_text, list_of_dangling_chunk_ids).
    """
    dangling: list[str] = []

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
