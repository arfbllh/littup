"""Stream an UploadFile to a temporary file, computing SHA256 in one pass."""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.core.errors import AppError

_CHUNK = 1024 * 1024  # 1 MiB


class FileTooLargeError(AppError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"Upload exceeds {limit} bytes (got {size})",
            code="FILE_TOO_LARGE",
            status_code=413,
        )


class EmptyFileError(AppError):
    def __init__(self) -> None:
        super().__init__("Upload is empty", code="EMPTY_FILE", status_code=400)


@dataclass(frozen=True)
class HashedUpload:
    path: Path
    sha256: str
    size_bytes: int


async def stream_to_tempfile(
    upload: UploadFile,
    *,
    tmp_dir: Path,
    max_bytes: int,
) -> HashedUpload:
    """Stream the upload to a tempfile under tmp_dir, hashing as we go.

    Aborts with FileTooLargeError if the running total exceeds max_bytes.
    Caller is responsible for unlinking the resulting path on failure
    paths after success.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    total = 0

    fd, tmp_path_str = tempfile.mkstemp(prefix="upload-", suffix=".tmp", dir=str(tmp_dir))
    tmp_path = Path(tmp_path_str)
    try:
        with open(fd, "wb") as out:
            while True:
                chunk = await upload.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLargeError(total, max_bytes)
                hasher.update(chunk)
                out.write(chunk)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    if total == 0:
        tmp_path.unlink(missing_ok=True)
        raise EmptyFileError()

    return HashedUpload(path=tmp_path, sha256=hasher.hexdigest(), size_bytes=total)
