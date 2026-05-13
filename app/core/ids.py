import hashlib
import uuid

from uuid_extensions import uuid7 as _uuid7


def new_uuid7() -> str:
    return str(_uuid7())


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_id() -> str:
    return uuid.uuid4().hex[:8]
