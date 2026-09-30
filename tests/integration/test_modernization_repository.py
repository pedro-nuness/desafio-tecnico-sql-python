from collections.abc import Callable
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.services.code_generation_service import CodeGenerationService
from app.application.services.modernization_service import ModernizationService
from app.domain.enums import ModernizationStatus, PipelineStep
from app.domain.exceptions import LLMProviderError, ModernizationNotFoundError
from app.domain.models.modernization import Modernization, PipelineError, PipelineOutcome
from app.domain.services.semantic_analyzer import SemanticAnalyzer
from app.graph.builder import build_modernization_graph
from app.graph.pipeline import LangGraphModernizationPipeline
from app.infrastructure.llm.fake_provider import FakeLLMProvider
from app.infrastructure.parsing.pglast_parser import PglastParser
from app.infrastructure.persistence.database.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.validation.composite_validator import CompositeCodeValidator
from app.infrastructure.validation.python_ast_validator import PythonASTValidator
from app.prompts.generation_prompt import GenerationPromptBuilder
from tests.conftest import llm_payload

pytestmark = pytest.mark.integration

type SessionFactory = async_sessionmaker[AsyncSession]


def _failed(modernization: Modernization) -> Modernization:
    outcome = PipelineOutcome(
        completed_steps=(PipelineStep.PARSING,),
        errors=(PipelineError(step=PipelineStep.GENERATION, error_type="X", message="boom"),),
    )
    return modernization.complete(outcome)


async def test_save_and_find_round_trip(session_factory: SessionFactory) -> None:
    modernization = Modernization.start("CREATE FUNCTION ...", "CREATE TABLE t();")

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.modernizations.save(modernization)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        found = await uow.modernizations.find_by_id(modernization.id)

    assert found == modernization
    assert found is not None and found.created_at.tzinfo is not None


async def test_update_persists_report_as_jsonb(session_factory: SessionFactory) -> None:
    modernization = Modernization.start("src")
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.modernizations.save(modernization)
        await uow.commit()

    finished = _failed(modernization)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.modernizations.update(finished)
        await uow.commit()

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
    modernization = Modernization.start("src")

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.modernizations.save(modernization)  # flushed, never committed

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.modernizations.find_by_id(modernization.id) is None


async def test_update_of_unknown_aggregate_raises(session_factory: SessionFactory) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        with pytest.raises(ModernizationNotFoundError):
            await uow.modernizations.update(
                Modernization.start("src").model_copy(update={"id": uuid4()})
            )


def _service(session_factory: SessionFactory, llm: FakeLLMProvider) -> ModernizationService:
    graph = build_modernization_graph(
        parser=PglastParser(),
        analyzer=SemanticAnalyzer(),
        generation_service=CodeGenerationService(llm, GenerationPromptBuilder()),
        validator=CompositeCodeValidator([PythonASTValidator()]),
    )
    return ModernizationService(
        LangGraphModernizationPipeline(graph), lambda: SqlAlchemyUnitOfWork(session_factory)
    )


async def test_every_execution_is_persisted_including_failures(
    session_factory: SessionFactory, load_procedure: Callable[[str], str]
) -> None:
    source = load_procedure("process_orders")
    ok = await _service(session_factory, FakeLLMProvider([llm_payload()])).modernize(source)
    ko = await _service(
        session_factory, FakeLLMProvider(error=LLMProviderError("timeout"))
    ).modernize(source)

    async with session_factory() as session:
        result = await session.execute(text("SELECT id, status FROM modernization_history"))
        rows = {row_id: status for row_id, status in result.all()}
    assert rows == {ok.id: "success", ko.id: "failure"}
    assert ok.status is ModernizationStatus.SUCCESS
    assert ko.report.completed_steps == (PipelineStep.PARSING, PipelineStep.SEMANTIC_ANALYSIS)
