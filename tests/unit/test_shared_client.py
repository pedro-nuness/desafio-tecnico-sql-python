import json

import httpx
import pytest

from app.shared.client import HttpClient


async def test_get_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.example.com/items?page=1"
        return httpx.Response(200, json={"items": [1, 2, 3]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.example.com"
    ) as mock_client:
        client = HttpClient(client=mock_client)
        resp = await client.get("/items", params={"page": 1})
        assert resp.status_code == 200
        assert resp.json() == {"items": [1, 2, 3]}


async def test_post_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.read()) == {"name": "test"}
        return httpx.Response(201, json={"id": 42})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.example.com"
    ) as mock_client:
        client = HttpClient(client=mock_client)
        resp = await client.post("/items", json={"name": "test"})
        assert resp.status_code == 201
        assert resp.json() == {"id": 42}


async def test_status_error_propagates_after_retries() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.example.com"
    ) as mock_client:
        client = HttpClient(client=mock_client, max_retries=2)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get("/flaky")
        assert exc_info.value.response.status_code == 500
        assert attempts == 3


async def test_native_timeout_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Operation timed out")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.example.com"
    ) as mock_client:
        client = HttpClient(client=mock_client, max_retries=1)
        with pytest.raises(httpx.TimeoutException):
            await client.get("/timeout")


async def test_native_network_error_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.NetworkError("DNS lookup failed")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.example.com"
    ) as mock_client:
        client = HttpClient(client=mock_client, max_retries=0)
        with pytest.raises(httpx.NetworkError):
            await client.get("/network-error")


async def test_put_patch_delete_helpers() -> None:
    methods_called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods_called.append(request.method)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.example.com"
    ) as mock_client:
        client = HttpClient(client=mock_client)
        await client.put("/resource", json={"a": 1})
        await client.patch("/resource", json={"b": 2})
        await client.delete("/resource")

    assert methods_called == ["PUT", "PATCH", "DELETE"]
