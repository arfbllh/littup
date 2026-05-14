from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import Callable

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chunk import Chunk
from app.ingest.chunker import extract_entities
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.dense import DenseRetriever
from app.retrieval.fusion import entity_overlap_bonus, rrf_fuse
from app.retrieval.reranker import RerankerWrapper
from app.retrieval.trigram import TrigramRetriever
from app.retrieval.types import RetrievedChunk
from app.settings import settings

logger = structlog.get_logger(__name__)

_PROPER_NOUN_RE = re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b')
_SECTION_RE = re.compile(r'§')


def _should_run_trigram(query: str) -> bool:
    if settings.RETRIEVER_ALWAYS_TRIGRAM:
        return True
    return bool(_PROPER_NOUN_RE.search(query) or _SECTION_RE.search(query))


class HybridRetriever:
    def __init__(
        self,
        session_factory: Callable,
        reranker: RerankerWrapper,
        embedder,
    ) -> None:
        self._sf = session_factory
        self._reranker = reranker
        self._embedder = embedder

    async def retrieve(
        self,
        query: str,
        document_ids: list[str] | None,
        top_k: int = 8,
        fetch_k: int = 50,
    ) -> list[Chunk]:
        run_trigram = _should_run_trigram(query)
        qvec = (await self._embedder.embed([query]))[0]

        async def do_bm25() -> list[tuple[str, float]]:
            async with self._sf() as s:
                return await BM25Retriever(s).search(query, document_ids, fetch_k)

        async def do_dense() -> list[tuple[str, float]]:
            async with self._sf() as s:
                return await DenseRetriever(s).search(qvec, document_ids, fetch_k)

        async def do_trigram() -> list[tuple[str, float]]:
            async with self._sf() as s:
                return await TrigramRetriever(s).search(query, document_ids, fetch_k)

        if run_trigram:
            bm25_res, dense_res, trgm_res = await asyncio.gather(
                do_bm25(), do_dense(), do_trigram()
            )
            fused = rrf_fuse([bm25_res, dense_res, trgm_res], weights=[1.0, 1.0, 0.5])
        else:
            bm25_res, dense_res = await asyncio.gather(do_bm25(), do_dense())
            fused = rrf_fuse([bm25_res, dense_res], weights=[1.0, 1.0])

        if not fused:
            return []

        candidate_ids = [cid for cid, _ in fused]
        fused_scores = dict(fused)

        chunk_map: dict[str, Chunk] = {}
        chunk_entities_map: dict[str, list[str]] = {}

        async with self._sf() as fetch_session:
            placeholders = ", ".join(f":id_{i}" for i in range(len(candidate_ids)))
            id_params = {f"id_{i}": cid for i, cid in enumerate(candidate_ids)}
            rows = (
                await fetch_session.execute(
                    text(
                        f"SELECT id, document_id, text, section_path, page_start, page_end,"
                        f" chunk_type, token_count, block_ids, entities, char_start, char_end,"
                        f' prompt_fingerprint, "metadata", created_at'
                        f" FROM app.chunks WHERE id IN ({placeholders})"
                    ),
                    id_params,
                )
            ).mappings().fetchall()

        for row in rows:
            c = Chunk()
            c.id = str(row["id"])
            c.document_id = str(row["document_id"])
            c.text = row["text"]
            c.section_path = row["section_path"]
            c.page_start = row["page_start"]
            c.page_end = row["page_end"]
            c.chunk_type = row["chunk_type"]
            c.token_count = row["token_count"]
            c.block_ids = row["block_ids"]
            c.entities = row["entities"]
            c.char_start = row["char_start"]
            c.char_end = row["char_end"]
            c.prompt_fingerprint = row["prompt_fingerprint"]
            c.metadata_ = row["metadata"]
            c.created_at = row["created_at"]
            chunk_map[c.id] = c
            chunk_entities_map[c.id] = list(c.entities or [])

        query_entities = extract_entities(query)
        bonuses = entity_overlap_bonus(query_entities, chunk_entities_map)
        for cid, bonus in bonuses.items():
            if cid in fused_scores:
                fused_scores[cid] += bonus

        re_sorted = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)

        per_doc: dict[str, int] = defaultdict(int)
        capped: list[tuple[str, float]] = []
        for cid, score in re_sorted:
            chunk = chunk_map.get(cid)
            if chunk is None:
                continue
            doc_id = chunk.document_id
            if per_doc[doc_id] >= settings.MAX_CHUNKS_PER_DOC_PER_QUERY:
                continue
            per_doc[doc_id] += 1
            capped.append((cid, score))
            if len(capped) >= fetch_k:
                break

        retrieved = [
            RetrievedChunk(chunk=chunk_map[cid], score=score)
            for cid, score in capped
            if cid in chunk_map
        ]

        reranked = await self._reranker.rerank(query, retrieved, top_k)
        return [rc.chunk for rc in reranked]

    async def multi_retrieve(
        self,
        queries: dict[str, str],
        document_ids: list[str] | None,
        top_k_per_query: int = 5,
    ) -> dict[str, list[Chunk]]:
        keys = list(queries.keys())
        results = await asyncio.gather(
            *[self.retrieve(queries[k], document_ids, top_k=top_k_per_query) for k in keys]
        )
        return dict(zip(keys, results))
