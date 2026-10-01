from collections.abc import Callable
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database.transaction import SessionTransactionManager
from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.domain.modernization import (
    Modernization,
    PipelineError,
    PipelineOutcome,
    PipelineProgress,
)
from app.features.modernization.domain.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.generation.generate_code import GenerateCode
from app.features.modernization.generation.prompt import GenerationPromptBuilder
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
from app.features.modernization.validation.python_ast_check import PythonASTCheck
from app.features.modernization.validation.validate_code import Rule, ValidateCode
from app.shared.errors import NotFoundError
from app.shared.integrations.errors import IntegrationError
from tests.conftest import llm_payload
from tests.fakes import FakeLLM

pytestmark = pytest.mark.integration

type SessionFactory = async_sessionmaker[AsyncSession]


def _failed(modernization: Modernization) -> Modernization:
    outcome = PipelineOutcome(
        completed_steps=(PipelineStep.PARSING,),
        errors=(PipelineError(step=PipelineStep.GENERATION, error_type="X", message="boom"),),
    )
    return modernization.complete(outcome)


async def test_save_and_get_round_trip(session_factory: SessionFactory) -> None:
    transactions, modernizations = _persistence(session_factory)
    modernization = Modernization.start("CREATE FUNCTION ...", "CREATE TABLE t();")

    async with transactions.transaction() as tx:
        await modernizations.save(modernization)
        await tx.commit()

    async with transactions.transaction():
        found = await modernizations.get(modernization.id)

    assert found == modernization
    assert found.created_at.tzinfo is not None


async def test_update_persists_report_as_jsonb(session_factory: SessionFactory) -> None:
    transactions, modernizations = _persistence(session_factory)
    modernization = Modernization.start("src")
    async with transactions.transaction() as tx:
        await modernizations.save(modernization)
        await tx.commit()

    finished = _failed(modernization)
    async with transactions.transaction() as tx:
        await modernizations.update(finished)
        await tx.commit()

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
    assert tuple(row) == ("failure", "object", "generation")


async def test_nothing_is_written_without_commit(session_factory: SessionFactory) -> None:
    transactions, modernizations = _persistence(session_factory)
    modernization = Modernization.start("src")

    async with transactions.transaction():
        await modernizations.save(modernization)  # flushed, never committed

    async with transactions.transaction():
        with pytest.raises(NotFoundError):
            await modernizations.get(modernization.id)


async def test_update_of_unknown_aggregate_raises(session_factory: SessionFactory) -> None:
    transactions, modernizations = _persistence(session_factory)
    async with transactions.transaction():
        with pytest.raises(NotFoundError):
            await modernizations.update(
                Modernization.start("src").model_copy(update={"id": uuid4()})
            )


def _persistence(
    session_factory: SessionFactory,
) -> tuple[SessionTransactionManager, SqlAlchemyModernizationRepository]:
    transactions = SessionTransactionManager(session_factory)
    return transactions, SqlAlchemyModernizationRepository(transactions)


def _use_cases(
    session_factory: SessionFactory, llm: FakeLLM
) -> tuple[ModernizeRoutine, GetModernization]:
    transactions, modernizations = _persistence(session_factory)
    graph = build_modernization_graph(
        parser=PglastParser(),
        analyzer=SemanticAnalyzer(),
        generate_code=GenerateCode(llm, GenerationPromptBuilder()),
        validate_code=ValidateCode([Rule(PythonASTCheck(), blocking=True)]),
        execution_log=ExecutionLog(transactions, modernizations),
    )
    return ModernizeRoutine(graph), GetModernization(transactions, modernizations)


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
