from __future__ import annotations
from dataclasses import dataclass
from app.db.models.chunk import Chunk


@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float
    degraded_mode: bool = False
