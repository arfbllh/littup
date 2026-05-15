"""Job handlers. Importing this package registers handlers into HANDLERS."""

from app.jobs.handlers import (
    chunking,  # noqa: F401
    draft_generation,  # noqa: F401
    embedding,  # noqa: F401
    few_shot_index,  # noqa: F401
    layout,  # noqa: F401
    ocr,  # noqa: F401  — registration side-effect
    page_reextract,  # noqa: F401
    reembed_chunks,  # noqa: F401
)
