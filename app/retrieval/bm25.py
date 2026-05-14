from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import structlog

logger = structlog.get_logger(__name__)


class BM25Retriever:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self, query: str, document_ids: list[str] | None, k: int
    ) -> list[tuple[str, float]]:
        if not query or not query.strip():
            return []

        sql = """
            SELECT c.id, ts_rank_cd(c.text_tsv, plainto_tsquery('legal_en', :query)) AS score
            FROM app.chunks c
            JOIN app.documents d ON d.id = c.document_id
            WHERE d.status = 'ready'
              AND c.text_tsv @@ plainto_tsquery('legal_en', :query)
        """
        params: dict = {"query": query, "k": k}

        if document_ids is not None:
            sql += " AND c.document_id = ANY(:doc_ids)"
            params["doc_ids"] = document_ids

        sql += " ORDER BY score DESC LIMIT :k"

        result = await self.session.execute(text(sql), params)
        rows = result.fetchall()

        log = logger.bind(query=query, k=k, returned=len(rows))
        log.debug("bm25.search")

        return [(str(row[0]), float(row[1])) for row in rows]
