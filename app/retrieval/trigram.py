from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import structlog

from app.settings import settings

logger = structlog.get_logger(__name__)


class TrigramRetriever:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self, query: str, document_ids: list[str] | None, k: int
    ) -> list[tuple[str, float]]:
        if not query or not query.strip():
            return []

        threshold = float(settings.TRIGRAM_THRESHOLD)
        if not (0.0 <= threshold <= 1.0):
            raise ValueError(f"Invalid TRIGRAM_THRESHOLD: {threshold!r}")

        # Use the % operator so the planner can use chunks_text_trgm_idx (GIN).
        # SET LOCAL pg_trgm.similarity_threshold controls the % threshold;
        # similarity() in the SELECT gives the actual score.
        sql = """
            SELECT c.id, similarity(c.text, :query) AS score
            FROM app.chunks c
            JOIN app.documents d ON d.id = c.document_id
            WHERE d.status = 'ready'
              AND c.text % :query
        """
        params: dict = {"query": query, "k": k}

        if document_ids is not None:
            sql += " AND c.document_id = ANY(:doc_ids)"
            params["doc_ids"] = document_ids

        sql += " ORDER BY score DESC LIMIT :k"

        ctx = (
            self.session.begin_nested()
            if self.session.in_transaction()
            else self.session.begin()
        )
        async with ctx:
            await self.session.execute(
                text(f"SET LOCAL pg_trgm.similarity_threshold = {threshold}")
            )
            result = await self.session.execute(text(sql), params)
            rows = result.fetchall()

        log = logger.bind(query=query, k=k, returned=len(rows))
        log.debug("trigram.search")

        return [(str(row[0]), float(row[1])) for row in rows]
