"""Composition root: dishka providers, the only place that knows every concrete adapter.

Every dependency is declared here once, as a factory of the type it provides. Entry points
never build objects: FastAPI routes ask for `FromDishka[T]`, the LangGraph server asks the
container for the graph (see bootstrap.py). All of it is process-wide (Scope.APP): one
engine (connection pool), one LLM gateway (circuit breakers), one graph.
"""

from collections.abc import AsyncIterator

from dishka import Provider, Scope, alias, from_context, provide
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config.settings import Settings
from app.core.database.engine import create_engine
from app.core.database.session import create_session_factory
from app.core.database.transaction import SessionTransactionManager
from app.features.modernization.application.ports.pipeline.modernization_pipeline import (
    ModernizationPipeline,
)
from app.features.modernization.application.ports.repositories.modernization_repository import (
    ModernizationRepository,
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
from app.features.modernization.infrastructure.persistence.repositories.sqlalchemy_modernization_repository import (  # noqa: E501
    SqlAlchemyModernizationRepository,
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
from app.features.modernization.prompts.generation_prompt import GenerationPromptBuilder
from app.shared.integrations.llm.gateway import LLMGateway
from app.shared.integrations.llm.llm import LLM
from app.shared.integrations.llm.registry import build_providers
from app.shared.persistence import TransactionManager


class InfrastructureProvider(Provider):
    """Process-wide resources shared by every feature."""

    scope = Scope.APP

    settings = from_context(provides=Settings, scope=Scope.APP)

    @provide
    async def engine(self, settings: Settings) -> AsyncIterator[AsyncEngine]:
        engine = create_engine(str(settings.database_url), echo=settings.database_echo)
        yield engine
        await engine.dispose()

    @provide
    def transactions(self, engine: AsyncEngine) -> SessionTransactionManager:
        return SessionTransactionManager(create_session_factory(engine))

    # Use cases depend on the port; repositories on the implementation (current_session()).
    transaction_port = alias(source=SessionTransactionManager, provides=TransactionManager)

    @provide
    def llm(self, settings: Settings) -> LLM:
        llm_settings = settings.llm_settings()
        return LLMGateway(
            build_providers(llm_settings, app_name=settings.app_name),
            llm_settings.routes,
            budget_seconds=llm_settings.budget_seconds,
        )


class ModernizationProvider(Provider):
    """The modernization feature: graph, use cases and their adapters."""

    scope = Scope.APP

    modernizations = provide(SqlAlchemyModernizationRepository, provides=ModernizationRepository)

    @provide
    def generation_service(self, settings: Settings, llm: LLM) -> CodeGenerationService:
        return CodeGenerationService(
            llm,
            GenerationPromptBuilder(),
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
        )

    @provide
    def graph(
        self,
        settings: Settings,
        generation_service: CodeGenerationService,
        transactions: TransactionManager,
        modernizations: ModernizationRepository,
    ) -> ModernizationGraph:
        validator = CompositeCodeValidator(
            [PythonASTValidator(), RuffValidator(timeout_seconds=settings.ruff_timeout_seconds)]
        )
        return build_modernization_graph(
            parser=PglastParser(),
            analyzer=SemanticAnalyzer(),
            generation_service=generation_service,
            validator=validator,
            transactions=transactions,
            modernizations=modernizations,
            retry=RetryPolicy(
                max_attempts=settings.generation_max_attempts,
                budget_seconds=settings.generation_retry_budget_seconds,
            ),
        )

    pipeline = provide(LangGraphModernizationPipeline, provides=ModernizationPipeline)
    service = provide(ModernizationService)
