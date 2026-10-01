import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.features.modernization.application.ports.parsing.sql_parser import SQLParser
from app.features.modernization.application.ports.repositories.modernization_repository import (
    ModernizationRepository,
)
from app.features.modernization.application.ports.validation.code_validator import (
    CodeValidator,
)
from app.features.modernization.application.services.code_generation_service import (
    CodeGenerationService,
)
from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.domain.models.modernization import PipelineProgress
from app.features.modernization.domain.services.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.graph.nodes.generation_node import GenerationNode
from app.features.modernization.graph.nodes.parsing_node import ParsingNode
from app.features.modernization.graph.nodes.persistence_nodes import (
    RecordFailure,
    RecordResultNode,
    RecordStartNode,
)
from app.features.modernization.graph.nodes.semantic_analysis_node import (
    SemanticAnalysisNode,
)
from app.features.modernization.graph.nodes.validation_node import ValidationNode
from app.features.modernization.graph.state import (
    ModernizationInput,
    ModernizationState,
    StateUpdate,
)
from app.shared.persistence import TransactionManager

type ModernizationGraph = CompiledStateGraph[ModernizationState, None, ModernizationInput]
type Node = Callable[[ModernizationState], StateUpdate | Awaitable[StateUpdate]]
type AsyncNode = Callable[[ModernizationState, RunnableConfig], Awaitable[StateUpdate]]

GRAPH_NAME = "modernization"
RECORD_START = "record_start"
RECORD_RESULT = "record_result"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """When validation rejects the code, generation runs again with the issues as feedback."""

    max_attempts: int = 2
    """Total generation attempts (1 = never retry)."""
    budget_seconds: float = 90.0
    """No new attempt starts once the run is older than this (bounds the synchronous wait)."""


DEFAULT_RETRY = RetryPolicy()


def build_modernization_graph(
    *,
    parser: SQLParser,
    analyzer: SemanticAnalyzer,
    generation_service: CodeGenerationService,
    validator: CodeValidator,
    transactions: TransactionManager,
    modernizations: ModernizationRepository,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> ModernizationGraph:
    """START -> record_start -> parsing -> semantic_analysis -> generation -> validation
    -> record_result -> END, with validation -> generation while retries remain.

    A step that raises is recorded as FAILURE (with everything produced so far) and the
    exception propagates to the caller: the global HTTP handler maps it to a response.
    Dependencies are injected into node instances (closures), keeping nodes thin.
    """
    graph = StateGraph(ModernizationState, input_schema=ModernizationInput)
    record_failure = RecordFailure(transactions, modernizations)
    graph.add_node(RECORD_START, _tracked(None, RecordStartNode(transactions, modernizations)))
    steps: list[tuple[PipelineStep, Node]] = [
        (PipelineStep.PARSING, ParsingNode(parser)),
        (PipelineStep.SEMANTIC_ANALYSIS, SemanticAnalysisNode(analyzer)),
        (PipelineStep.GENERATION, GenerationNode(generation_service)),
        (PipelineStep.VALIDATION, ValidationNode(validator)),
    ]
    for step, node in steps:
        graph.add_node(step.value, _tracked(step, node, record_failure))
    graph.add_node(RECORD_RESULT, _tracked(None, RecordResultNode(transactions, modernizations)))

    graph.add_edge(START, RECORD_START)
    graph.add_edge(RECORD_START, PipelineStep.PARSING.value)
    graph.add_edge(PipelineStep.PARSING.value, PipelineStep.SEMANTIC_ANALYSIS.value)
    graph.add_edge(PipelineStep.SEMANTIC_ANALYSIS.value, PipelineStep.GENERATION.value)
    graph.add_edge(PipelineStep.GENERATION.value, PipelineStep.VALIDATION.value)
    graph.add_conditional_edges(
        PipelineStep.VALIDATION.value,
        _after_validation(retry),
        [PipelineStep.GENERATION.value, RECORD_RESULT],
    )
    graph.add_edge(RECORD_RESULT, END)
    return graph.compile(name=GRAPH_NAME)


def _tracked(
    step: PipelineStep | None, node: Node, record_failure: RecordFailure | None = None
) -> AsyncNode:
    """Expose the execution id to the caller and record a failing step before re-raising.

    Persistence nodes (step=None) are not recorded: a run that cannot be persisted must
    fail loudly.
    """

    async def run(state: ModernizationState, config: RunnableConfig) -> StateUpdate:
        progress = config.get("configurable", {}).get("progress")
        if isinstance(progress, PipelineProgress):
            progress.execution_id = state.get("execution_id")
        # Needed: the only local handler of the pipeline. It persists, never swallows.
        try:
            update = node(state)
            return await update if inspect.isawaitable(update) else update
        except Exception as exc:
            if step is not None and record_failure is not None:
                await record_failure(state, step, exc)
            raise

    return run


def _after_validation(retry: RetryPolicy) -> Callable[[ModernizationState], str]:
    def route(state: ModernizationState) -> str:
        validation = state.get("validation_result")
        if (
            state.get("status") is ModernizationStatus.FAILURE
            or validation is None
            or validation.passed_all
            or state.get("generation_attempts", 0) >= retry.max_attempts
        ):
            return RECORD_RESULT
        elapsed = (datetime.now(UTC) - state["started_at"]).total_seconds()
        return PipelineStep.GENERATION.value if elapsed < retry.budget_seconds else RECORD_RESULT

    return route
