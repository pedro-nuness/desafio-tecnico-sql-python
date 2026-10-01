"""Graph-level behaviour: validation -> generation retry loop and run recording."""

from collections.abc import Callable

import pytest

from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.domain.modernization import PipelineProgress
from app.features.modernization.graph.builder import RetryPolicy
from app.features.modernization.use_cases import ModernizeCommand
from app.shared.integrations.errors import IntegrationError
from tests.conftest import VALID_CODE, GraphFactory, ModernizeFactory, llm_payload
from tests.fakes import FakeLLM, InMemoryDatabase

LINT_ONLY = "import os\n\nvalue = 1\n"
BROKEN = llm_payload(code="def broken(:\n")


async def test_rejected_code_is_regenerated_with_validation_feedback(
    make_modernize: ModernizeFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLM([BROKEN, llm_payload()])

    result = await make_modernize(llm=llm).execute(
        ModernizeCommand(load_procedure("process_orders"))
    )

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
    make_modernize: ModernizeFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLM([BROKEN])  # always broken

    use_case = make_modernize(llm=llm, retry=RetryPolicy(max_attempts=3))
    result = await use_case.execute(ModernizeCommand(load_procedure("process_orders")))

    assert result.status is ModernizationStatus.FAILURE
    assert len(llm.requests) == 3
    assert result.report.generation is not None and result.report.generation.attempt == 3


async def test_no_retry_once_the_time_budget_is_spent(
    make_modernize: ModernizeFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLM([BROKEN, llm_payload()])

    use_case = make_modernize(llm=llm, retry=RetryPolicy(max_attempts=3, budget_seconds=0))
    result = await use_case.execute(ModernizeCommand(load_procedure("process_orders")))

    assert result.status is ModernizationStatus.FAILURE
    assert len(llm.requests) == 1


async def test_failed_retry_keeps_the_previous_attempt(
    make_modernize: ModernizeFactory, store: InMemoryDatabase, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLM([llm_payload(code=LINT_ONLY), "not json"])

    progress = PipelineProgress()
    with pytest.raises(IntegrationError):
        await make_modernize(llm=llm).execute(
            ModernizeCommand(load_procedure("calculate_discount")), progress=progress
        )
    assert progress.execution_id is not None
    recorded = store.rows[progress.execution_id]
    assert recorded.status is ModernizationStatus.FAILURE
    assert recorded.generated_code == LINT_ONLY
    [error] = recorded.report.errors
    assert (error.step, error.error_type) == (PipelineStep.GENERATION, "IntegrationError")


async def test_failing_runs_started_on_the_graph_are_recorded(
    make_graph: GraphFactory, store: InMemoryDatabase, load_procedure: Callable[[str], str]
) -> None:
    """LangGraph API / Studio path: no FastAPI handler around it, the graph records it."""
    llm = FakeLLM(error=RuntimeError("provider exploded"))

    with pytest.raises(RuntimeError, match="provider exploded"):
        await make_graph(llm=llm).ainvoke({"source_code": load_procedure("process_orders")})

    assert [m.status for m in store.history] == [
        ModernizationStatus.RUNNING,
        ModernizationStatus.FAILURE,
    ]
    [error] = store.history[-1].report.errors
    assert (error.step, error.error_type) == (PipelineStep.GENERATION, "RuntimeError")


async def test_runs_started_on_the_graph_are_persisted(
    make_graph: GraphFactory, store: InMemoryDatabase, load_procedure: Callable[[str], str]
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
