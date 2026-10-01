from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import pytest
from dishka import Provider, Scope
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.bootstrap import build_container
from app.core.config.settings import Settings
from app.core.server import create_app
from app.features.modernization.domain import ModernizationStatus
from app.features.modernization.evaluation.repository import EvaluationRepository
from app.features.modernization.persistence.repository import ModernizationRepository
from app.features.modernization.use_cases import ModernizeRoutine
from app.features.modernization.validation.checks.behavior.domain import CaseResult
from app.features.modernization.validation.checks.behavior.harness import EquivalenceMetric
from app.features.modernization.validation.domain import ValidationMessage
from app.features.modernization.validation.validate_code import Rule, ValidateCode
from app.shared.errors import AppError, DomainError, NotFoundError
from app.shared.integrations.errors import IntegrationError
from tests.conftest import ModernizeFactory
from tests.fakes import (
    FakeLLM,
    FakeMetric,
    InMemoryDatabase,
    InMemoryEvaluationRepository,
    InMemoryModernizationRepository,
)

type ApiFactory = Callable[..., FastAPI]


@pytest.fixture
def make_api(make_modernize: ModernizeFactory, store: InMemoryDatabase) -> ApiFactory:
    """The real container (core/providers.py) with the use cases and persistence overridden
    by ones wired to the in-memory store and fakes; graph options as in `make_graph`."""

    def factory(metric: FakeMetric | None = None, **graph_options: Any) -> FastAPI:
        metric = metric or FakeMetric()
        fakes = Provider(scope=Scope.APP)
        fakes.provide(lambda: make_modernize(**graph_options), provides=ModernizeRoutine)
        fakes.provide(
            lambda: InMemoryModernizationRepository(store), provides=ModernizationRepository
        )
        fakes.provide(lambda: InMemoryEvaluationRepository(store), provides=EvaluationRepository)
        fakes.provide(lambda: metric, provides=EquivalenceMetric)
        return create_app(build_container(Settings(_env_file=None), fakes))  # type: ignore[call-arg]

    return factory


@pytest.fixture
def api(make_api: ApiFactory) -> FastAPI:
    return make_api()


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
    assert all(check["success"] for check in body["report"]["validation"]["results"])

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
    make_api: ApiFactory,
    client: AsyncClient,
    store: InMemoryDatabase,
    load_procedure: Callable[[str], str],
    error: Exception,
    status_code: int,
) -> None:
    failing = make_api(llm=FakeLLM(error=error))
    async with AsyncClient(
        transport=ASGITransport(app=failing, raise_app_exceptions=False), base_url="http://test"
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
    make_api: ApiFactory,
    client: AsyncClient,
    load_procedure: Callable[[str], str],
    response_content: str,
) -> None:
    off_contract = make_api(llm=FakeLLM([response_content]))
    async with AsyncClient(
        transport=ASGITransport(app=off_contract), base_url="http://test"
    ) as http:
        response = await http.post(
            "/modernize", json={"source_code": load_procedure("process_orders")}
        )
    assert response.status_code == 502
    stored = (await client.get(f"/modernizations/{response.json()['execution_id']}")).json()
    assert stored["status"] == "failure"
    assert stored["report"]["errors"][0]["step"] == "generation"


async def test_validation_exception_keeps_generated_code_before_http_response(
    make_api: ApiFactory,
    client: AsyncClient,
    load_procedure: Callable[[str], str],
) -> None:
    import json

    class CrashingCheck:
        name = "crashing"

        async def check(self, code: str, routine: object = None) -> tuple[ValidationMessage, ...]:
            raise RuntimeError("validator failed")

    code = "def broken(:\n"
    crashing = make_api(
        llm=FakeLLM([json.dumps({"python_code": code})]),
        validate_code=ValidateCode([Rule(CrashingCheck(), blocking=True)]),
    )
    async with AsyncClient(
        transport=ASGITransport(app=crashing, raise_app_exceptions=False), base_url="http://test"
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


async def test_evaluation_of_a_recorded_execution_is_stored_and_summarized(
    make_api: ApiFactory, load_procedure: Callable[[str], str]
) -> None:
    cases = [
        CaseResult(name=n, passed=p, detail="", original="", generated="")
        for n, p in [("ok", True), ("rounding", False)]
    ]
    api = make_api(metric=FakeMetric(cases))
    async with api.router.lifespan_context(api):
        transport = ASGITransport(app=api)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            created = await http.post(
                "/modernize", json={"source_code": load_procedure("process_orders")}
            )
            execution_id = created.json()["execution_id"]

            evaluation = await http.post(f"/modernizations/{execution_id}/evaluation")
            summary = await http.get("/evaluations")

    assert evaluation.status_code == 200
    body = evaluation.json()
    assert body["execution_id"] == execution_id
    assert body["procedure_name"] == "process_customer_orders"
    assert (body["cases_passed"], body["cases_total"], body["score"]) == (1, 2, 0.5)
    assert body["equivalent"] is False and body["metric"] == "behavioral_equivalence"
    assert summary.status_code == 200
    assert summary.json()["routines"] == 1
    assert summary.json()["case_pass_rate"] == 0.5
    assert summary.json()["evaluations"][0]["evaluation_id"] == body["evaluation_id"]


async def test_evaluating_an_unknown_execution_returns_404(client: AsyncClient) -> None:
    response = await client.post(f"/modernizations/{uuid4()}/evaluation")

    assert response.status_code == 404
