from __future__ import annotations

import re

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import settings

logger = structlog.get_logger(__name__)

_WORK_MEM_RE = re.compile(r'^\d+[KMG]?B$')
_TIMEOUT_RE = re.compile(r'^\d+m?s$')


class DenseRetriever:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self, query_embedding: list[float], document_ids: list[str] | None, k: int
    ) -> list[tuple[str, float]]:
        if not query_embedding:
            return []

        if not _WORK_MEM_RE.match(settings.RETRIEVAL_WORK_MEM):
            raise ValueError(f"Invalid RETRIEVAL_WORK_MEM: {settings.RETRIEVAL_WORK_MEM!r}")
        if not _TIMEOUT_RE.match(settings.RETRIEVAL_STATEMENT_TIMEOUT):
            raise ValueError(
                f"Invalid RETRIEVAL_STATEMENT_TIMEOUT: {settings.RETRIEVAL_STATEMENT_TIMEOUT!r}"
            )

        qvec = "[" + ",".join(str(f) for f in query_embedding) + "]"

        sql = """
            SELECT c.id, (1 - (c.embedding <=> CAST(:qvec AS vector))) AS score
            FROM app.chunks c
            JOIN app.documents d ON d.id = c.document_id
            WHERE d.status = 'ready'
        """
        params: dict = {"qvec": qvec, "k": k}

        if document_ids is not None:
            sql += " AND c.document_id = ANY(:doc_ids)"
            params["doc_ids"] = document_ids

        sql += " ORDER BY c.embedding <=> CAST(:qvec AS vector) LIMIT :k"

        ctx = (
            self.session.begin_nested()
            if self.session.in_transaction()
            else self.session.begin()
        )
        async with ctx:
            await self.session.execute(
                text(f"SET LOCAL hnsw.ef_search = {settings.HNSW_EF_SEARCH}")
            )
            await self.session.execute(
                text(f"SET LOCAL work_mem = '{settings.RETRIEVAL_WORK_MEM}'")
            )
            await self.session.execute(
                text(f"SET LOCAL statement_timeout = '{settings.RETRIEVAL_STATEMENT_TIMEOUT}'")
            )
            result = await self.session.execute(text(sql), params)
            rows = result.fetchall()

        logger.debug("dense.search", k=k, returned=len(rows))
        return [(str(r.id), float(r.score)) for r in rows]
