from collections.abc import Callable
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.features.modernization.analysis.analyzer import SemanticAnalyzer
from app.features.modernization.code_generation.generate_code import GenerateCode
from app.features.modernization.code_generation.prompt import CodeGenerationPromptBuilder
from app.features.modernization.domain import (
    Modernization,
    ModernizationReport,
    ModernizationStatus,
    PipelineError,
    PipelineProgress,
    PipelineStep,
)
from app.features.modernization.graph.builder import build_modernization_graph
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.persistence.execution_log import ExecutionLog
from app.features.modernization.persistence.repository import SqlAlchemyModernizationRepository
from app.features.modernization.use_cases import (
    GetModernization,
    GetModernizationQuery,
    ModernizeCommand,
    ModernizeRoutine,
)
from app.features.modernization.validation.checks.syntax import PythonASTCheck
from app.features.modernization.validation.validate_code import Rule, ValidateCode
from app.shared.errors import NotFoundError
from app.shared.integrations.errors import IntegrationError
from tests.conftest import llm_payload
from tests.fakes import FakeLLM

pytestmark = pytest.mark.integration

type SessionFactory = async_sessionmaker[AsyncSession]


def _failed(modernization: Modernization) -> Modernization:
    report = ModernizationReport(
        completed_steps=(PipelineStep.PARSING,),
        errors=(PipelineError(step=PipelineStep.CODE_GENERATION, error_type="X", message="boom"),),
    )
    return modernization.complete(report, None)


async def test_save_and_get_round_trip(session_factory: SessionFactory) -> None:
    modernizations = SqlAlchemyModernizationRepository(session_factory)
    modernization = Modernization.start("CREATE FUNCTION ...", "CREATE TABLE t();")

    await modernizations.save(modernization)
    found = await modernizations.get(modernization.id)

    assert found == modernization
    assert found.created_at.tzinfo is not None


async def test_update_persists_report_as_jsonb(session_factory: SessionFactory) -> None:
    modernizations = SqlAlchemyModernizationRepository(session_factory)
    modernization = Modernization.start("src")
    await modernizations.save(modernization)

    await modernizations.update(_failed(modernization))

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, jsonb_typeof(report), report -> 'errors' -> 0 ->> 'step' "
                    "FROM modernization_history WHERE id = :id"
                ),
                {"id": modernization.id},
            )
        ).one()
    assert tuple(row) == ("failure", "object", "code_generation")


async def test_update_of_unknown_aggregate_raises(session_factory: SessionFactory) -> None:
    modernizations = SqlAlchemyModernizationRepository(session_factory)
    with pytest.raises(NotFoundError):
        await modernizations.update(Modernization.start("src").model_copy(update={"id": uuid4()}))


def _use_cases(
    session_factory: SessionFactory, llm: FakeLLM
) -> tuple[ModernizeRoutine, GetModernization]:
    modernizations = SqlAlchemyModernizationRepository(session_factory)
    graph = build_modernization_graph(
        parser=PglastParser(),
        analyzer=SemanticAnalyzer(),
        generate_code=GenerateCode(llm, CodeGenerationPromptBuilder()),
        validate_code=ValidateCode([Rule(PythonASTCheck(), blocking=True)]),
        execution_log=ExecutionLog(modernizations),
    )
    return ModernizeRoutine(graph), GetModernization(modernizations)


async def test_every_execution_is_persisted_including_failures(
    session_factory: SessionFactory, load_procedure: Callable[[str], str]
) -> None:
    source = load_procedure("process_orders")
    modernize, _ = _use_cases(session_factory, FakeLLM([llm_payload()]))
    ok = await modernize.execute(ModernizeCommand(source))
    failing, get = _use_cases(session_factory, FakeLLM(error=IntegrationError("timeout")))
    progress = PipelineProgress()
    with pytest.raises(IntegrationError):
        await failing.execute(ModernizeCommand(source), progress=progress)
    assert progress.execution_id is not None
    # recorded by the graph before re-raising
    ko = await get.execute(GetModernizationQuery(progress.execution_id))

    async with session_factory() as session:
        result = await session.execute(text("SELECT id, status FROM modernization_history"))
        rows = {row_id: status for row_id, status in result.all()}
    assert rows == {ok.id: "success", ko.id: "failure"}
    assert ok.status is ModernizationStatus.SUCCESS
    assert ko.report.completed_steps == (PipelineStep.PARSING, PipelineStep.SEMANTIC_ANALYSIS)
