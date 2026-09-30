import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.application.ports.parsing.sql_parser import SQLParser
from app.application.ports.repositories.unit_of_work import UnitOfWorkFactory
from app.application.ports.validation.code_validator import CodeValidator
from app.application.services.code_generation_service import CodeGenerationService
from app.domain.enums import ModernizationStatus, PipelineStep
from app.domain.services.semantic_analyzer import SemanticAnalyzer
from app.graph.nodes.generation_node import GenerationNode
from app.graph.nodes.parsing_node import ParsingNode
from app.graph.nodes.persistence_nodes import RecordResultNode, RecordStartNode
from app.graph.nodes.semantic_analysis_node import SemanticAnalysisNode
from app.graph.nodes.validation_node import ValidationNode
from app.graph.state import ModernizationInput, ModernizationState, StateUpdate, failed

logger = logging.getLogger(__name__)

type ModernizationGraph = CompiledStateGraph[ModernizationState, None, ModernizationInput]
type Node = Callable[[ModernizationState], StateUpdate | Awaitable[StateUpdate]]
type AsyncNode = Callable[[ModernizationState], Awaitable[StateUpdate]]

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
    uow_factory: UnitOfWorkFactory,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> ModernizationGraph:
    """START -> record_start -> parsing -> semantic_analysis -> generation -> validation
    -> record_result -> END, with validation -> generation while retries remain.

    A failed step short-circuits to record_result, so every run is persisted.
    Dependencies are injected into node instances (closures), keeping nodes thin.
    """
    graph = StateGraph(ModernizationState, input_schema=ModernizationInput)
    graph.add_node(RECORD_START, RecordStartNode(uow_factory))
    steps: list[tuple[PipelineStep, Node]] = [
        (PipelineStep.PARSING, ParsingNode(parser)),
        (PipelineStep.SEMANTIC_ANALYSIS, SemanticAnalysisNode(analyzer)),
        (PipelineStep.GENERATION, GenerationNode(generation_service)),
        (PipelineStep.VALIDATION, ValidationNode(validator)),
    ]
    for step, node in steps:
        graph.add_node(step.value, _guarded(step, node))
    graph.add_node(RECORD_RESULT, RecordResultNode(uow_factory))

    graph.add_edge(START, RECORD_START)
    graph.add_edge(RECORD_START, PipelineStep.PARSING.value)
    _continue_unless_failed(graph, PipelineStep.PARSING, PipelineStep.SEMANTIC_ANALYSIS)
    _continue_unless_failed(graph, PipelineStep.SEMANTIC_ANALYSIS, PipelineStep.GENERATION)
    _continue_unless_failed(graph, PipelineStep.GENERATION, PipelineStep.VALIDATION)
    graph.add_conditional_edges(
        PipelineStep.VALIDATION.value,
        _after_validation(retry),
        [PipelineStep.GENERATION.value, RECORD_RESULT],
    )
    graph.add_edge(RECORD_RESULT, END)
    return graph.compile(name=GRAPH_NAME)


def _guarded(step: PipelineStep, node: Node) -> AsyncNode:
    """Unexpected exceptions become a PipelineError of `step` (expected ones are handled by
    the node itself), so the run still reaches record_result with everything done so far."""

    async def run(state: ModernizationState) -> StateUpdate:
        try:
            update = node(state)
            return await update if inspect.isawaitable(update) else update
        except Exception as exc:
            logger.exception("unexpected error in node %s", step.value)
            return failed(step, exc)

    return run


def _continue_unless_failed(
    graph: StateGraph[ModernizationState, None, ModernizationInput],
    source: PipelineStep,
    target: PipelineStep,
) -> None:
    def route(state: ModernizationState) -> str:
        failed_ = state.get("status") is ModernizationStatus.FAILURE
        return RECORD_RESULT if failed_ else target.value

    graph.add_conditional_edges(source.value, route, [target.value, RECORD_RESULT])


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
