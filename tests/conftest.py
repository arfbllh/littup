import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run tests that hit real LLM provider APIs (costs money).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="needs --live flag (hits real provider APIs)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
