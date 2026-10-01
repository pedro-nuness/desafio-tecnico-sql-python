from collections.abc import Callable

import pytest
from pglast.parser import ParseError

from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.domain.exceptions import ModernizationNotFoundError
from app.features.modernization.domain.models.modernization import PipelineProgress
from app.features.modernization.domain.models.validation import ValidationResult
from app.shared.integrations.exceptions import IntegrationError
from tests.conftest import ServiceFactory, llm_payload
from tests.fakes import FakeLLMProvider, InMemoryStore

ALL_STEPS = tuple(PipelineStep)


async def test_success_runs_all_four_nodes_and_persists_twice(
    make_service: ServiceFactory, store: InMemoryStore, load_procedure: Callable[[str], str]
) -> None:
    result = await make_service().modernize(load_procedure("process_orders"), "CREATE TABLE t();")

    assert result.status is ModernizationStatus.SUCCESS
    assert result.report.completed_steps == ALL_STEPS
    assert result.generated_code is not None
    report = result.report
    assert report.parsing is not None and report.parsing.success
    assert report.semantic_analysis is not None
    assert report.generation is not None and report.generation.provider == "fake"
    assert report.validation is not None and report.validation.valid_python
    # RUNNING row committed before the pipeline, final row committed after it
    assert [m.status for m in store.history] == [
        ModernizationStatus.RUNNING,
        ModernizationStatus.SUCCESS,
    ]
    assert store.rows[result.id] == result


async def test_llm_failure_propagates_with_progress_for_the_handler(
    make_service: ServiceFactory, store: InMemoryStore, load_procedure: Callable[[str], str]
) -> None:
    error = IntegrationError("provider unavailable")
    service = make_service(llm=FakeLLMProvider(error=error))
    progress = PipelineProgress()
    with pytest.raises(IntegrationError) as exc_info:
        await service.modernize(load_procedure("process_orders"), progress=progress)
    assert exc_info.value is error
    assert progress.step is PipelineStep.GENERATION
    assert progress.outcome.completed_steps == (
        PipelineStep.PARSING,
        PipelineStep.SEMANTIC_ANALYSIS,
    )
    assert store.rows[progress.execution_id].status is ModernizationStatus.RUNNING


async def test_parsing_failure_propagates_without_calling_the_llm(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLMProvider()
    with pytest.raises(ParseError):
        await make_service(llm=llm).modernize(load_procedure("invalid_syntax"))
    assert not llm.requests


async def test_invalid_python_is_a_failure_but_code_is_kept(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLMProvider([llm_payload(code="def broken(:\n")])

    result = await make_service(llm=llm).modernize(load_procedure("calculate_discount"))

    assert result.status is ModernizationStatus.FAILURE
    assert result.generated_code == "def broken(:\n"
    assert result.report.validation is not None and not result.report.validation.valid_python


async def test_lint_findings_make_the_result_partial(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    llm = FakeLLMProvider([llm_payload(code="import os\n\nvalue = 1\n")])

    result = await make_service(llm=llm).modernize(load_procedure("calculate_discount"))

    assert result.status is ModernizationStatus.PARTIAL
    assert result.report.validation is not None
    assert any("F401" in warning for warning in result.report.validation.warnings)


class _ExplodingValidator:
    name = "exploding"

    async def validate(self, code: str) -> ValidationResult:
        raise RuntimeError("unexpected bug")


async def test_unexpected_crash_propagates_with_last_known_state(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    progress = PipelineProgress()
    with pytest.raises(RuntimeError, match="unexpected bug"):
        await make_service(validator=_ExplodingValidator()).modernize(
            load_procedure("process_orders"),
            progress=progress,
        )
    assert progress.outcome.generated_code is not None
    assert progress.outcome.completed_steps == ALL_STEPS[:3]
    assert progress.step is PipelineStep.VALIDATION


async def test_get_returns_persisted_execution(
    make_service: ServiceFactory, load_procedure: Callable[[str], str]
) -> None:
    service = make_service()
    created = await service.modernize(load_procedure("calculate_discount"))

    assert await service.get(created.id) == created


async def test_get_unknown_execution_raises(make_service: ServiceFactory) -> None:
    from uuid import uuid4

    with pytest.raises(ModernizationNotFoundError):
        await make_service().get(uuid4())
