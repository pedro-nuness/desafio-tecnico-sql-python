from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, ConnectError, ReadTimeout, Request, Response
from openai import APIError, APITimeoutError
from openrouter.errors import OpenRouterError

from app.core.bootstrap import Container
from app.core.config.settings import Settings
from app.core.server import create_app
from app.features.modernization.application.services.modernization_service import (
    ModernizationService,
)
from app.features.modernization.domain.enums import ModernizationStatus
from app.features.modernization.graph.pipeline import LangGraphModernizationPipeline
from app.shared.integrations.exceptions import IntegrationError
from app.shared.resilience.circuit_breaker import CircuitOpenError
from tests.conftest import GraphFactory
from tests.fakes import FakeLLMProvider, InMemoryStore


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


async def test_parsing_failure_returns_http_error_and_is_persisted(
    client: AsyncClient, load_procedure: Callable[[str], str]
) -> None:
    response = await client.post(
        "/modernize", json={"source_code": load_procedure("invalid_syntax")}
    )

    assert response.status_code == 400
    body = response.json()
    stored = await client.get(f"/modernizations/{body['execution_id']}")
    assert stored.status_code == 200
    report = stored.json()
    assert report["status"] == "failure"
    assert report["generated_code"] is None
    assert report["report"]["errors"][0]["step"] == "parsing"
    assert report["report"]["errors"][0]["error_type"] == "ParseError"


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
    @api.get("/test-timeout")
    async def route_timeout() -> None:
        raise ReadTimeout("External service timeout")

    @api.get("/test-connection")
    async def route_connection() -> None:
        raise ConnectError("Failed to connect")

    @api.get("/test-upstream-500")
    async def route_upstream() -> None:
        Response(500, request=Request("GET", "https://example.com")).raise_for_status()

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


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (IntegrationError("private failure"), 502),
        (CircuitOpenError("llm", 30), 503),
        (APIError("private failure", Request("POST", "https://example.com"), body=None), 502),
        (APITimeoutError(Request("POST", "https://example.com")), 504),
        (OpenRouterError("private failure", Response(500, text="private failure")), 502),
        (OpenRouterError("private failure", Response(408, text="private failure")), 504),
        (RuntimeError("private failure"), 500),
    ],
)
async def test_global_handler_persists_sdk_failures_with_progress(
    api: FastAPI,
    client: AsyncClient,
    make_graph: GraphFactory,
    store: InMemoryStore,
    load_procedure: Callable[[str], str],
    error: Exception,
    status_code: int,
) -> None:
    api.state.container.modernization_service._pipeline._graph = make_graph(
        llm=FakeLLMProvider(error=error)
    )
    async with AsyncClient(
        transport=ASGITransport(app=api, raise_app_exceptions=False), base_url="http://test"
    ) as http:
        response = await http.post(
            "/modernize", json={"source_code": load_procedure("process_orders")}
        )
    assert response.status_code == status_code
    if isinstance(error, CircuitOpenError):
        assert response.headers["Retry-After"] == "30"
    body = response.json()
    assert "private failure" not in body["detail"]
    stored_response = await client.get(f"/modernizations/{body['execution_id']}")
    stored = stored_response.json()
    assert stored_response.status_code == 200
    assert stored["status"] == "failure"
    assert stored["report"]["completed_steps"] == ["parsing", "semantic_analysis"]
    assert stored["report"]["errors"][0]["error_type"] == type(error).__name__
    assert stored["report"]["errors"][0]["step"] == "generation"
    assert [m.status for m in store.history] == [
        ModernizationStatus.RUNNING,
        ModernizationStatus.FAILURE,
    ]


@pytest.mark.parametrize(
    "response_content", ["no json", '{"python_code":', '{"strategy": "hybrid"}']
)
async def test_invalid_llm_payload_reaches_the_handler(
    api: FastAPI,
    client: AsyncClient,
    make_graph: GraphFactory,
    load_procedure: Callable[[str], str],
    response_content: str,
) -> None:
    api.state.container.modernization_service._pipeline._graph = make_graph(
        llm=FakeLLMProvider([response_content])
    )
    response = await client.post(
        "/modernize", json={"source_code": load_procedure("process_orders")}
    )
    assert response.status_code == 502
    stored = (await client.get(f"/modernizations/{response.json()['execution_id']}")).json()
    assert stored["status"] == "failure"
    assert stored["report"]["errors"][0]["step"] == "generation"


async def test_validation_exception_keeps_generated_code_before_http_response(
    api: FastAPI,
    client: AsyncClient,
    make_graph: GraphFactory,
    load_procedure: Callable[[str], str],
) -> None:
    import json

    class CrashingValidator:
        name = "crashing"

        async def validate(self, code: str):
            raise RuntimeError("validator failed")

    code = "def broken(:\n"
    api.state.container.modernization_service._pipeline._graph = make_graph(
        llm=FakeLLMProvider([json.dumps({"python_code": code})]),
        validator=CrashingValidator(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=api, raise_app_exceptions=False), base_url="http://test"
    ) as http:
        response = await http.post(
            "/modernize", json={"source_code": load_procedure("process_orders")}
        )
    assert response.status_code == 500
    stored = (await client.get(f"/modernizations/{response.json()['execution_id']}")).json()
    assert stored["status"] == "failure"
    assert stored["generated_code"] == code
    assert stored["report"]["completed_steps"] == ["parsing", "semantic_analysis", "generation"]
    assert stored["report"]["errors"][0]["step"] == "validation"
    assert stored["report"]["errors"][0]["error_type"] == "RuntimeError"
