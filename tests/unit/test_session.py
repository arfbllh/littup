"""
Unit tests for DB session lifecycle (no live DB required — tests the generator logic).
"""

import contextlib
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_get_session_commits_on_success():
    mock_session = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.session.async_session_factory", return_value=mock_cm):
        from app.db.session import get_session

        gen = get_session()
        session = await gen.__anext__()
        assert session is mock_session

        with contextlib.suppress(StopAsyncIteration):
            await gen.asend(None)

        mock_session.commit.assert_awaited()
        mock_session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_session_rolls_back_on_exception():
    mock_session = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.session.async_session_factory", return_value=mock_cm):
        from app.db.session import get_session

        gen = get_session()
        await gen.__anext__()

        with pytest.raises(RuntimeError):
            await gen.athrow(RuntimeError("boom"))

        mock_session.rollback.assert_awaited()
