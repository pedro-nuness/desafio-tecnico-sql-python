"""Composition root: the only place that knows every concrete adapter.

Explicit constructor wiring (no DI framework). `build_container` assembles everything once;
`default_container` caches that per process so both entry points served by `langgraph dev`
(the FastAPI lifespan and the LangGraph server via `make_graph`) share one engine and one
graph. Tests and scripts call `build_container` directly (or inject their own Container).
"""

from dataclasses import dataclass
from functools import cache

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config.settings import Settings
from app.core.database.engine import create_engine
from app.core.database.session import create_session_factory
from app.features.modernization.application.ports.repositories.unit_of_work import (
    UnitOfWorkFactory,
)
from app.features.modernization.application.services.code_generation_service import (
    CodeGenerationService,
)
from app.features.modernization.application.services.modernization_service import (
    ModernizationService,
)
from app.features.modernization.domain.services.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.graph.builder import (
    ModernizationGraph,
    RetryPolicy,
    build_modernization_graph,
)
from app.features.modernization.graph.pipeline import LangGraphModernizationPipeline
from app.features.modernization.infrastructure.parsing.pglast_parser import PglastParser
from app.features.modernization.infrastructure.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from app.features.modernization.infrastructure.validation.composite_validator import (
    CompositeCodeValidator,
)
from app.features.modernization.infrastructure.validation.python_ast_validator import (
    PythonASTValidator,
)
from app.features.modernization.infrastructure.validation.ruff_validator import (
    RuffValidator,
)
from app.features.modernization.prompts.generation_prompt import (
    GenerationPromptBuilder,
)
from app.shared.integrations.llm.factory import create_llm_provider


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    graph: ModernizationGraph
    modernization_service: ModernizationService
    engine: AsyncEngine | None = None

    async def aclose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


def build_graph(settings: Settings, uow_factory: UnitOfWorkFactory) -> ModernizationGraph:
    generation_service = CodeGenerationService(
        create_llm_provider(settings.llm_config()),
        GenerationPromptBuilder(),
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    validator = CompositeCodeValidator(
        [PythonASTValidator(), RuffValidator(timeout_seconds=settings.ruff_timeout_seconds)]
    )
    return build_modernization_graph(
        parser=PglastParser(),
        analyzer=SemanticAnalyzer(),
        generation_service=generation_service,
        validator=validator,
        uow_factory=uow_factory,
        retry=RetryPolicy(
            max_attempts=settings.generation_max_attempts,
            budget_seconds=settings.generation_retry_budget_seconds,
        ),
    )


def _uow_factory(engine: AsyncEngine) -> UnitOfWorkFactory:
    session_factory = create_session_factory(engine)
    return lambda: SqlAlchemyUnitOfWork(session_factory)


def build_container(settings: Settings) -> Container:
    engine = create_engine(str(settings.database_url), echo=settings.database_echo)
    uow_factory = _uow_factory(engine)
    graph = build_graph(settings, uow_factory)
    service = ModernizationService(
        pipeline=LangGraphModernizationPipeline(graph), uow_factory=uow_factory
    )
    return Container(settings=settings, graph=graph, modernization_service=service, engine=engine)


@cache
def default_container() -> Container:
    """The process-wide container: one engine (connection pool), one graph, one Settings."""
    return build_container(Settings())


def make_graph() -> ModernizationGraph:
    """Graph factory referenced by langgraph.json (LangGraph API / Studio).

    Same graph the FastAPI routes use, so runs started there are persisted the same way.
    """
    return default_container().graph
