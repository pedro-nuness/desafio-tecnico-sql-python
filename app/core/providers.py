"""Composition root: dishka providers, the only place that knows every concrete adapter.

Every dependency is declared here once, as a factory of the type it provides. Entry points
never build objects: FastAPI routes ask for `FromDishka[T]`, the LangGraph server asks the
container for the graph (see bootstrap.py). All of it is process-wide (Scope.APP): one
engine (connection pool), one LLM gateway (circuit breakers), one graph.
"""

from collections.abc import AsyncIterator

from dishka import Provider, Scope, alias, from_context, provide
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config.settings import Settings
from app.core.database.engine import create_engine
from app.core.database.session import create_session_factory
from app.features.modernization.domain.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.evaluation.equivalence import (
    BehavioralEquivalence,
    EquivalenceMetric,
)
from app.features.modernization.evaluation.repository import (
    EvaluationRepository,
    SqlAlchemyEvaluationRepository,
)
from app.features.modernization.generation.generate_code import GenerateCode
from app.features.modernization.generation.prompt import GenerationPromptBuilder
from app.features.modernization.graph.builder import (
    ModernizationGraph,
    RetryPolicy,
    build_modernization_graph,
)
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.persistence.execution_log import ExecutionLog
from app.features.modernization.persistence.repository import (
    ModernizationRepository,
    SqlAlchemyModernizationRepository,
)
from app.features.modernization.use_cases import (
    EvaluateModernization,
    GetEvaluationSummary,
    GetModernization,
    ModernizeRoutine,
)
from app.features.modernization.validation.behavior_check import BehaviorCheck
from app.features.modernization.validation.python_ast_check import PythonASTCheck
from app.features.modernization.validation.ruff_check import RuffCheck
from app.features.modernization.validation.validate_code import Rule, ValidateCode
from app.shared.integrations.llm.gateway import LLMGateway
from app.shared.integrations.llm.llm import LLM
from app.shared.integrations.llm.registry import build_providers
from app.shared.integrations.llm.tracing import TracedLLM


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
    def sessions(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return create_session_factory(engine)

    @provide
    async def langfuse(self, settings: Settings) -> AsyncIterator[Langfuse | None]:
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            yield None
            return
        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
        )
        yield client
        client.shutdown()

    @provide
    def llm(self, settings: Settings, langfuse: Langfuse | None) -> LLM:
        llm_settings = settings.llm_settings()
        gateway = LLMGateway(
            build_providers(llm_settings, app_name=settings.app_name),
            llm_settings.routes,
            budget_seconds=llm_settings.budget_seconds,
        )
        return TracedLLM(gateway) if langfuse else gateway


class ModernizationProvider(Provider):
    """The modernization feature: use cases, graph, steps and strategies."""

    scope = Scope.APP

    modernizations = provide(SqlAlchemyModernizationRepository, provides=ModernizationRepository)
    execution_log = provide(ExecutionLog)

    @provide
    def generate_code(self, settings: Settings, llm: LLM) -> GenerateCode:
        return GenerateCode(
            llm,
            GenerationPromptBuilder(),
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
        )

    @provide
    def validate_code(self, settings: Settings, equivalence: BehavioralEquivalence) -> ValidateCode:
        # The policy: invalid syntax makes the code unusable; lint findings and behavior
        # divergences make it PARTIAL. All of them trigger the repair loop (AD-13).
        return ValidateCode(
            [
                Rule(PythonASTCheck(), blocking=True),
                Rule(RuffCheck(timeout_seconds=settings.ruff_timeout_seconds), blocking=False),
                Rule(BehaviorCheck(equivalence), blocking=False),
            ]
        )

    @provide
    def graph(
        self,
        settings: Settings,
        generate_code: GenerateCode,
        validate_code: ValidateCode,
        execution_log: ExecutionLog,
        langfuse: Langfuse | None,
    ) -> ModernizationGraph:
        graph = build_modernization_graph(
            parser=PglastParser(),
            analyzer=SemanticAnalyzer(),
            generate_code=generate_code,
            validate_code=validate_code,
            execution_log=execution_log,
            retry=RetryPolicy(
                max_attempts=settings.generation_max_attempts,
                budget_seconds=settings.generation_retry_budget_seconds,
            ),
        )
        if langfuse:
            graph = graph.with_config(
                callbacks=[CallbackHandler(public_key=settings.langfuse_public_key)]
            )
        return graph

    evaluations = provide(SqlAlchemyEvaluationRepository, provides=EvaluationRepository)

    @provide
    async def equivalence(self, settings: Settings) -> AsyncIterator[BehavioralEquivalence]:
        url = settings.evaluation_database_url
        harness = BehavioralEquivalence(
            str(url) if url else None,
            settings.evaluation_dataset_file,
            case_timeout_seconds=settings.evaluation_case_timeout_seconds,
        )
        yield harness
        await harness.close()

    # The same harness is the metric (use cases) and the behavior check (validation).
    equivalence_metric = alias(source=BehavioralEquivalence, provides=EquivalenceMetric)

    modernize = provide(ModernizeRoutine)
    get_modernization = provide(GetModernization)
    evaluate = provide(EvaluateModernization)
    evaluation_summary = provide(GetEvaluationSummary)
