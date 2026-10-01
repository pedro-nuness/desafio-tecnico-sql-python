"""Graph-level behaviour: validation -> generation retry loop and run recording."""

from collections.abc import Callable

from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.graph.builder import RetryPolicy
from tests.conftest import VALID_CODE, GraphFactory, ServiceFactory, llm_payload
from tests.fakes import FakeLLMProvider, InMemoryStore

BROKEN = llm_payload(code="def broken(:\n")
LINT_ONLY = "import os\n\nvalue = 1\n"


async def test_rejected_code_is_regenerated_with_validation_feedback(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLMProvider([BROKEN, llm_payload()])

    result = await make_service(llm=llm).modernize(load_procedure("process_orders"))

    assert result.status is ModernizationStatus.SUCCESS
    assert result.generated_code == VALID_CODE
    first, retry = llm.requests
    assert "rejected by validation" not in first.user_prompt
    assert "Attempt 2" in retry.user_prompt
    assert "[python_ast]" in retry.user_prompt and "def broken(:" in retry.user_prompt
    assert result.report.generation is not None and result.report.generation.attempt == 2
    generation, validation = PipelineStep.GENERATION, PipelineStep.VALIDATION
    assert result.report.completed_steps[2:] == (generation, validation, generation, validation)
    assert any("generation attempt 2" in warning for warning in result.report.warnings)


async def test_retries_stop_at_max_attempts(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLMProvider([BROKEN])  # always broken

    service = make_service(llm=llm, retry=RetryPolicy(max_attempts=3))
    result = await service.modernize(load_procedure("process_orders"))

    assert result.status is ModernizationStatus.FAILURE
    assert len(llm.requests) == 3
    assert result.report.generation is not None and result.report.generation.attempt == 3


async def test_no_retry_once_the_time_budget_is_spent(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLMProvider([BROKEN, llm_payload()])

    service = make_service(llm=llm, retry=RetryPolicy(max_attempts=3, budget_seconds=0))
    result = await service.modernize(load_procedure("process_orders"))

    assert result.status is ModernizationStatus.FAILURE
    assert len(llm.requests) == 1


async def test_failed_retry_keeps_the_previous_attempt(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLMProvider([llm_payload(code=LINT_ONLY), "not json"])

    result = await make_service(llm=llm).modernize(load_procedure("calculate_discount"))

    assert result.status is ModernizationStatus.PARTIAL  # attempt 1 is still usable
    assert result.generated_code == LINT_ONLY
    [error] = result.report.errors
    assert error.step is PipelineStep.GENERATION


async def test_runs_started_on_the_graph_are_persisted(
    make_graph: GraphFactory, store: InMemoryStore, load_procedure: Callable[[str], str]
) -> None:
    """The LangGraph API / Studio path: only the input fields, no service around it."""
    final = await make_graph().ainvoke({"source_code": load_procedure("process_orders")})

    modernization = final["modernization"]
    assert modernization.status is ModernizationStatus.SUCCESS
    assert store.rows[modernization.id] == modernization
    assert [m.status for m in store.history] == [
        ModernizationStatus.RUNNING,
        ModernizationStatus.SUCCESS,
    ]
