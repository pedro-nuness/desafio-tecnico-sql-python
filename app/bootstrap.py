"""Composition root: the only place that knows every concrete adapter.

Explicit constructor wiring (no DI container, no global singletons): each entry point
(FastAPI lifespan, LangGraph CLI, tests) builds exactly what it needs.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.services.code_generation_service import CodeGenerationService
from app.application.services.modernization_service import ModernizationService
from app.config.settings import Settings
from app.domain.services.semantic_analyzer import SemanticAnalyzer
from app.graph.builder import ModernizationGraph, build_modernization_graph
from app.graph.pipeline import LangGraphModernizationPipeline
from app.infrastructure.llm.provider_factory import create_llm_provider
from app.infrastructure.parsing.pglast_parser import PglastParser
from app.infrastructure.persistence.database.engine import create_engine
from app.infrastructure.persistence.database.session import create_session_factory
from app.infrastructure.persistence.database.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.validation.composite_validator import CompositeCodeValidator
from app.infrastructure.validation.python_ast_validator import PythonASTValidator
from app.infrastructure.validation.ruff_validator import RuffValidator
from app.prompts.generation_prompt import GenerationPromptBuilder


@dataclass(frozen=True, slots=True)
class Container:
    modernization_service: ModernizationService
    engine: AsyncEngine | None = None

    async def aclose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


def build_graph(settings: Settings) -> ModernizationGraph:
    generation_service = CodeGenerationService(
        create_llm_provider(settings),
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
    )


def build_container(settings: Settings) -> Container:
    engine = create_engine(str(settings.database_url), echo=settings.database_echo)
    session_factory = create_session_factory(engine)
    service = ModernizationService(
        pipeline=LangGraphModernizationPipeline(build_graph(settings)),
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
    )
    return Container(modernization_service=service, engine=engine)


def make_graph() -> ModernizationGraph:
    """Graph factory referenced by langgraph.json (LangGraph CLI / Studio)."""
    return build_graph(Settings())
