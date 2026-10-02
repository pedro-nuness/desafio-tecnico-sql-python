from collections.abc import Callable
from uuid import uuid4

import pytest

from app.features.modernization.domain import ModernizationStatus, PipelineProgress, PipelineStep
from app.features.modernization.use_cases import (
    GetModernization,
    GetModernizationQuery,
    ModernizeCommand,
)
from app.features.modernization.validation.domain import ValidationMessage
from app.features.modernization.validation.validate_code import Rule, ValidateCode
from app.shared.errors import DomainError, NotFoundError
from app.shared.integrations.errors import IntegrationError
from tests.conftest import ModernizeFactory, llm_payload
from tests.fakes import FakeLLM, InMemoryDatabase

# The graph built by make_graph has no case generator (see test_case_generation.py).
ALL_STEPS = tuple(step for step in PipelineStep if step is not PipelineStep.CASE_GENERATION)


async def test_success_runs_all_four_nodes_and_persists_twice(
    make_modernize: ModernizeFactory,
    store: InMemoryDatabase,
    load_procedure: Callable[[str], str],
) -> None:
    command = ModernizeCommand(load_procedure("process_orders"), "CREATE TABLE t();")

    result = await make_modernize().execute(command)

    assert result.status is ModernizationStatus.SUCCESS
    assert result.report.completed_steps == ALL_STEPS
    assert result.generated_code is not None
    report = result.report
    assert report.parsing is not None
    assert report.semantic_analysis is not None
    assert report.generation is not None and report.generation.provider == "fake"
    assert report.validation is not None and report.validation.is_valid
    # RUNNING row committed before the pipeline, final row committed after it
    assert [m.status for m in store.history] == [
        ModernizationStatus.RUNNING,
        ModernizationStatus.SUCCESS,
    ]
    assert store.rows[result.id] == result


async def test_llm_failure_is_recorded_then_propagates(
    make_modernize: ModernizeFactory,
    store: InMemoryDatabase,
    load_procedure: Callable[[str], str],
) -> None:
    error = IntegrationError("provider unavailable")
    use_case = make_modernize(llm=FakeLLM(error=error))
    progress = PipelineProgress()
    with pytest.raises(IntegrationError) as exc_info:
        await use_case.execute(
            ModernizeCommand(load_procedure("process_orders")), progress=progress
        )
    assert exc_info.value is error
    assert progress.execution_id is not None
    recorded = store.rows[progress.execution_id]
    assert recorded.status is ModernizationStatus.FAILURE
    assert recorded.report.completed_steps == (
        PipelineStep.PARSING,
        PipelineStep.SEMANTIC_ANALYSIS,
    )
    [recorded_error] = recorded.report.errors
    assert (recorded_error.step, recorded_error.error_type) == (
        PipelineStep.GENERATION,
        "IntegrationError",
    )


async def test_parsing_failure_propagates_without_calling_the_llm(
    make_modernize: ModernizeFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLM()
    with pytest.raises(DomainError):
        await make_modernize(llm=llm).execute(ModernizeCommand(load_procedure("invalid_syntax")))
    assert not llm.requests


async def test_invalid_python_is_a_failure_but_code_is_kept(
    make_modernize: ModernizeFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLM([llm_payload(code="def broken(:\n")])

    result = await make_modernize(llm=llm).execute(
        ModernizeCommand(load_procedure("calculate_discount"))
    )

    assert result.status is ModernizationStatus.FAILURE
    assert result.generated_code == "def broken(:\n"
    assert result.report.validation is not None and not result.report.validation.is_valid


async def test_lint_findings_make_the_result_partial(
    make_modernize: ModernizeFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLM([llm_payload(code="import os\n\nvalue = 1\n")])

    result = await make_modernize(llm=llm).execute(
        ModernizeCommand(load_procedure("calculate_discount"))
    )

    assert result.status is ModernizationStatus.PARTIAL
    assert result.report.validation is not None
    assert any("F401" in warning for warning in result.report.warnings)


class _ExplodingCheck:
    name = "exploding"

    async def check(self, code: str, routine: object = None) -> tuple[ValidationMessage, ...]:
        raise RuntimeError("unexpected bug")


async def test_unexpected_crash_is_recorded_with_last_known_state(
    make_modernize: ModernizeFactory,
    store: InMemoryDatabase,
    load_procedure: Callable[[str], str],
) -> None:
    exploding = ValidateCode([Rule(_ExplodingCheck(), blocking=True)])
    progress = PipelineProgress()
    with pytest.raises(RuntimeError, match="unexpected bug"):
        await make_modernize(validate_code=exploding).execute(
            ModernizeCommand(load_procedure("process_orders")), progress=progress
        )
    assert progress.execution_id is not None
    recorded = store.rows[progress.execution_id]
    assert recorded.status is ModernizationStatus.FAILURE
    assert recorded.generated_code is not None
    assert recorded.report.completed_steps == ALL_STEPS[:3]
    [error] = recorded.report.errors
    assert error.step is PipelineStep.VALIDATION


async def test_get_returns_persisted_execution(
    make_modernize: ModernizeFactory,
    get_modernization: GetModernization,
    load_procedure: Callable[[str], str],
) -> None:
    created = await make_modernize().execute(ModernizeCommand(load_procedure("calculate_discount")))

    assert await get_modernization.execute(GetModernizationQuery(created.id)) == created


async def test_get_unknown_execution_raises(get_modernization: GetModernization) -> None:
    with pytest.raises(NotFoundError):
        await get_modernization.execute(GetModernizationQuery(uuid4()))
