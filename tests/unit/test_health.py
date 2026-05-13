import pytest


@pytest.mark.asyncio
async def test_healthz_returns_200(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_echoes_request_id(client):
    response = await client.get("/healthz", headers={"X-Request-ID": "test-123"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-123"


@pytest.mark.asyncio
async def test_healthz_generates_request_id_when_absent(client):
    response = await client.get("/healthz")
    assert "X-Request-ID" in response.headers
    assert len(response.headers["X-Request-ID"]) > 0
