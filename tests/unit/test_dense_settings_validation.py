from __future__ import annotations
import pytest


@pytest.mark.asyncio
async def test_invalid_work_mem_raises(monkeypatch):
    from app.retrieval.dense import DenseRetriever
    from app.settings import settings

    monkeypatch.setattr(settings, "RETRIEVAL_WORK_MEM", "64 MB")  # space = invalid

    retriever = DenseRetriever(object())  # session never reached
    with pytest.raises(ValueError, match="Invalid RETRIEVAL_WORK_MEM"):
        await retriever.search([0.1] * 5, None, 5)


@pytest.mark.asyncio
async def test_invalid_statement_timeout_raises(monkeypatch):
    from app.retrieval.dense import DenseRetriever
    from app.settings import settings

    monkeypatch.setattr(settings, "RETRIEVAL_STATEMENT_TIMEOUT", "5 seconds")  # invalid

    retriever = DenseRetriever(object())
    with pytest.raises(ValueError, match="Invalid RETRIEVAL_STATEMENT_TIMEOUT"):
        await retriever.search([0.1] * 5, None, 5)


@pytest.mark.asyncio
async def test_work_mem_missing_unit_raises(monkeypatch):
    from app.retrieval.dense import DenseRetriever
    from app.settings import settings

    monkeypatch.setattr(settings, "RETRIEVAL_WORK_MEM", "64")  # no unit

    retriever = DenseRetriever(object())
    with pytest.raises(ValueError, match="Invalid RETRIEVAL_WORK_MEM"):
        await retriever.search([0.1] * 5, None, 5)


@pytest.mark.asyncio
async def test_valid_settings_pass_validation(monkeypatch):
    """Settings that match the regexes should not raise before hitting the DB."""
    from app.retrieval.dense import DenseRetriever
    from app.settings import settings
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(settings, "RETRIEVAL_WORK_MEM", "128MB")
    monkeypatch.setattr(settings, "RETRIEVAL_STATEMENT_TIMEOUT", "10s")
    monkeypatch.setattr(settings, "HNSW_EF_SEARCH", 100)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=None)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.in_transaction.return_value = False
    mock_session.begin.return_value = mock_ctx
    mock_session.execute = AsyncMock(return_value=MagicMock(fetchall=lambda: []))

    retriever = DenseRetriever(mock_session)
    result = await retriever.search([0.1] * 5, None, 5)
    assert result == []
