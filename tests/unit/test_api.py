from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.bootstrap import Container
from app.core.config.settings import Settings
from app.core.server import create_app
from app.features.modernization.application.services.modernization_service import (
    ModernizationService,
)
from app.features.modernization.graph.pipeline import LangGraphModernizationPipeline
from tests.conftest import GraphFactory
from tests.fakes import InMemoryStore


@pytest.fixture
def api(make_graph: GraphFactory, store: InMemoryStore) -> FastAPI:
    graph = make_graph()
    container = Container(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        graph=graph,
        modernization_service=ModernizationService(
            LangGraphModernizationPipeline(graph), store.uow
        ),
    )
    return create_app(container_factory=lambda: container)


@pytest.fixture
async def client(api: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api.router.lifespan_context(api):
        transport = ASGITransport(app=api)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_modernize_returns_structured_report(
    client: AsyncClient, load_procedure: Callable[[str], str]
) -> None:
    response = await client.post(
        "/modernize",
        json={"source_code": load_procedure("process_orders"), "schema": "CREATE TABLE t();"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["generated_code"]
    assert set(body["report"]) >= {"parsing", "semantic_analysis", "generation", "validation"}
    assert body["report"]["parsing"]["procedure_name"] == "billing.process_customer_orders"
    assert body["report"]["generation"]["strategy"] == "hybrid"
    assert body["report"]["semantic_analysis"]["recommended_strategy"] == "hybrid"
    assert body["report"]["validation"]["valid_python"] is True

    stored = await client.get(f"/modernizations/{body['execution_id']}")
    assert stored.status_code == 200
    assert stored.json()["report"] == body["report"]


async def test_schema_is_optional_and_failures_are_reported_in_the_body(
    client: AsyncClient, load_procedure: Callable[[str], str]
) -> None:
    response = await client.post(
        "/modernize", json={"source_code": load_procedure("invalid_syntax")}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failure"
    assert body["generated_code"] is None
    assert body["report"]["errors"][0]["step"] == "parsing"


async def test_empty_source_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/modernize", json={"source_code": ""})

    assert response.status_code == 422


async def test_unknown_execution_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/modernizations/{uuid4()}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


async def test_global_interceptor_handles_http_client_errors(
    api: FastAPI, client: AsyncClient
) -> None:
    from app.shared.client import HttpConnectionError, HttpResponseError, HttpTimeoutError

    @api.get("/test-timeout")
    async def route_timeout() -> None:
        raise HttpTimeoutError("External service timeout")

    @api.get("/test-connection")
    async def route_connection() -> None:
        raise HttpConnectionError("Failed to connect")

    @api.get("/test-upstream-500")
    async def route_upstream() -> None:
        raise HttpResponseError(status_code=500, message="Service unavailable")

    @api.get("/test-unhandled")
    async def route_unhandled() -> None:
        raise RuntimeError("Unexpected failure")

    timeout_resp = await client.get("/test-timeout")
    assert timeout_resp.status_code == 504
    assert timeout_resp.json() == {"detail": "Gateway Timeout: upstream service timed out"}

    conn_resp = await client.get("/test-connection")
    assert conn_resp.status_code == 502
    assert conn_resp.json() == {"detail": "Bad Gateway: failed to connect to upstream service"}

    upstream_resp = await client.get("/test-upstream-500")
    assert upstream_resp.status_code == 502
    assert "upstream service error (500)" in upstream_resp.json()["detail"]

    async with AsyncClient(
        transport=ASGITransport(app=api, raise_app_exceptions=False), base_url="http://test"
    ) as unhandled_client:
        unhandled_resp = await unhandled_client.get("/test-unhandled")
        assert unhandled_resp.status_code == 500
        assert unhandled_resp.json() == {"detail": "Internal Server Error"}
