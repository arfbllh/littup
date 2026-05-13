import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.main import create_app


@pytest.fixture
def app_with_error_routes():
    application = create_app()
    test_router = APIRouter()

    @test_router.get("/test/not-found")
    async def raise_not_found():
        raise NotFoundError("doc not found", code="DOCUMENT_NOT_FOUND")

    @test_router.get("/test/conflict")
    async def raise_conflict():
        raise ConflictError("already exists", code="ALREADY_EXISTS")

    @test_router.get("/test/validation")
    async def raise_validation():
        raise ValidationError("bad input", code="BAD_INPUT")

    application.include_router(test_router)
    return application


@pytest.mark.asyncio
async def test_not_found_error_renders_json_envelope(app_with_error_routes):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_error_routes), base_url="http://test"
    ) as ac:
        response = await ac.get("/test/not-found", headers={"X-Request-ID": "req-abc"})

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert body["error"]["message"] == "doc not found"
    assert body["error"]["request_id"] == "req-abc"


@pytest.mark.asyncio
async def test_conflict_error_status_code(app_with_error_routes):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_error_routes), base_url="http://test"
    ) as ac:
        response = await ac.get("/test/conflict")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_validation_error_status_code(app_with_error_routes):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_error_routes), base_url="http://test"
    ) as ac:
        response = await ac.get("/test/validation")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BAD_INPUT"


@pytest.mark.asyncio
async def test_error_response_contains_request_id_in_header(app_with_error_routes):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_error_routes), base_url="http://test"
    ) as ac:
        response = await ac.get("/test/not-found")

    assert "X-Request-ID" in response.headers
