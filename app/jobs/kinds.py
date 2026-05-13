from collections.abc import Callable
from enum import StrEnum

from app.core.errors import IngestError


class JobKind(StrEnum):
    OCR = "ocr"
    LAYOUT = "layout"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    RULE_EXTRACTION = "rule_extraction"
    FEW_SHOT_INDEX = "few_shot_index"


# Handlers registered by each milestone as they are implemented.
# Key: JobKind, Value: async callable(payload: dict, session) -> dict
HANDLERS: dict[str, Callable] = {}


def get_handler(kind: str) -> Callable:
    handler = HANDLERS.get(kind)
    if handler is None:
        raise IngestError(f"No handler registered for job kind '{kind}'", code="HANDLER_NOT_FOUND")
    return handler
