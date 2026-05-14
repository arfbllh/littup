"""Job handlers. Importing this package registers handlers into HANDLERS."""

from app.jobs.handlers import ocr  # noqa: F401  — registration side-effect
from app.jobs.handlers import layout  # noqa: F401
from app.jobs.handlers import chunking  # noqa: F401
from app.jobs.handlers import embedding  # noqa: F401
