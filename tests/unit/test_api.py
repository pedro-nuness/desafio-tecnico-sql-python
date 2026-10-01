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
from app.features.modernization.domain.enums import ModernizationStatus
from app.features.modernization.graph.pipeline import LangGraphModernizationPipeline
from app.shared.errors import AppError, DomainError, NotFoundError
from app.shared.integrations.errors import IntegrationError
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
    assert report["report"]["errors"][0]["error_type"] == "DomainError"


async def test_empty_source_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/modernize", json={"source_code": ""})

    assert response.status_code == 422


async def test_unknown_execution_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/modernizations/{uuid4()}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    ("error", "status_code", "body"),
    [
        (DomainError("Bad input", line=3), 400, {"detail": "Bad input", "line": 3}),
        (NotFoundError("Nothing here"), 404, {"detail": "Nothing here"}),
        (IntegrationError("llm failed"), 502, {"detail": "llm failed"}),
        (IntegrationError("llm timed out", timeout=True), 504, {"detail": "llm timed out"}),
        (IntegrationError("llm down", retry_after=29.2), 503, {"detail": "llm down"}),
        (
            AppError("Ruff failed to run", returncode=2),
            500,
            {"detail": "Ruff failed to run", "returncode": 2},
        ),
        (RuntimeError("private failure"), 500, {"detail": "Internal Server Error"}),
    ],
)
async def test_global_handler_maps_error_class_to_status_and_payload_to_body(
    api: FastAPI, error: Exception, status_code: int, body: dict
) -> None:
    @api.get("/boom")
    async def boom() -> None:
        raise error

    async with AsyncClient(
        transport=ASGITransport(app=api, raise_app_exceptions=False), base_url="http://test"
    ) as http:
        response = await http.get("/boom")

    assert response.status_code == status_code
    assert response.json() == body
    if status_code == 503:
        assert response.headers["Retry-After"] == "30"


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (IntegrationError("openrouter answered HTTP 500", upstream_status=500), 502),
        (IntegrationError("openrouter is unavailable", retry_after=30), 503),
        (IntegrationError("openrouter timed out", timeout=True), 504),
        (RuntimeError("private failure"), 500),
    ],
)
async def test_global_handler_maps_failures_and_points_to_the_recorded_run(
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
    if status_code == 503:
        assert response.headers["Retry-After"] == "30"
    body = response.json()
    assert "private failure" not in body["detail"]
    stored_response = await client.get(f"/modernizations/{body['execution_id']}")
    stored = stored_response.json()
    assert stored_response.status_code == 200
    assert stored["status"] == "failure"
    assert stored["report"]["completed_steps"] == ["parsing", "semantic_analysis"]
    [recorded_error] = stored["report"]["errors"]
    assert recorded_error["error_type"] == type(error).__name__
    assert recorded_error["step"] == "generation"
    if isinstance(error, IntegrationError):
        assert recorded_error["payload"] == error.payload
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
