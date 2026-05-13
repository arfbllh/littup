"""Local blob storage layout for ingested documents and rendered page images."""

from __future__ import annotations

import os
from pathlib import Path

from app.settings import settings


class LocalBlobStore:
    """Filesystem layout for uploads and page images.

    Uploads live under UPLOAD_DIR/{sha[:2]}/{sha}{ext}; temporary
    in-flight files live under UPLOAD_DIR/.tmp/ so os.replace into the
    final path stays atomic (same filesystem).
    """

    def __init__(
        self,
        upload_dir: str | None = None,
        page_image_dir: str | None = None,
    ) -> None:
        self.upload_dir = Path(upload_dir or settings.UPLOAD_DIR)
        self.page_image_dir = Path(page_image_dir or settings.PAGE_IMAGE_DIR)

    # ── Uploads ───────────────────────────────────────────────────────
    @property
    def tmp_dir(self) -> Path:
        return self.upload_dir / ".tmp"

    def path_for(self, sha256: str, ext: str) -> Path:
        if not ext.startswith("."):
            ext = "." + ext
        return self.upload_dir / sha256[:2] / f"{sha256}{ext}"

    def commit(self, tmp_path: Path, final_path: Path) -> None:
        """Atomically move tmp_path → final_path (same filesystem)."""
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, final_path)

    # ── Page images ───────────────────────────────────────────────────
    def page_image_path(self, document_id: str, page_n: int) -> Path:
        return self.page_image_dir / document_id / f"{page_n}.png"
