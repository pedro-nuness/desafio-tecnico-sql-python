from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.domain.modernization import (
    Modernization,
    PipelineError,
    PipelineProgress,
)
from app.features.modernization.domain.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.generation.generate_code import GenerateCode
from app.features.modernization.graph.nodes import (
    GenerationNode,
    ParsingNode,
    RecordResultNode,
    RecordStartNode,
    SemanticAnalysisNode,
    StepNode,
    ValidationNode,
)
from app.features.modernization.graph.state import (
    ModernizationInput,
    ModernizationState,
    StateUpdate,
    to_outcome,
)
from app.features.modernization.parsing.strategy import SQLParser
from app.features.modernization.persistence.execution_log import ExecutionLog
from app.features.modernization.validation.validate_code import ValidateCode

type ModernizationGraph = CompiledStateGraph[ModernizationState, None, ModernizationInput]
type Node = Callable[[ModernizationState], Awaitable[StateUpdate]]
type TrackedNode = Callable[[ModernizationState, RunnableConfig], Awaitable[StateUpdate]]

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
    generate_code: GenerateCode,
    validate_code: ValidateCode,
    execution_log: ExecutionLog,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> ModernizationGraph:
    """START -> record_start -> parsing -> semantic_analysis -> generation -> validation
    -> record_result -> END, with validation -> generation while retries remain.

    A step that raises is recorded as FAILURE (with everything produced so far) and the
    exception propagates to the caller: the global HTTP handler maps it to a response.
    """
    graph = StateGraph(ModernizationState, input_schema=ModernizationInput)
    steps: list[StepNode] = [
        ParsingNode(parser),
        SemanticAnalysisNode(analyzer),
        GenerationNode(generate_code),
        ValidationNode(validate_code),
    ]
    graph.add_node(RECORD_START, _tracked(RecordStartNode(execution_log)))
    for node in steps:
        graph.add_node(node.step.value, _tracked(node, node.step, execution_log))
    graph.add_node(RECORD_RESULT, _tracked(RecordResultNode(execution_log)))

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


async def run_modernization(
    graph: ModernizationGraph,
    *,
    source_code: str,
    schema_context: str | None,
    progress: PipelineProgress | None = None,
) -> Modernization:
    """Invokes the graph; `progress` receives the execution id as soon as it exists."""
    final = await graph.ainvoke(
        ModernizationInput(source_code=source_code, schema_context=schema_context),
        config={"configurable": {"progress": progress}},
    )
    modernization: Modernization = final["modernization"]
    return modernization


def _tracked(
    node: Node, step: PipelineStep | None = None, execution_log: ExecutionLog | None = None
) -> TrackedNode:
    """Expose the execution id to the caller and record a failing step before re-raising.

    Recording nodes (no step) are not recorded: a run that cannot be persisted must fail
    loudly.
    """

    async def run(state: ModernizationState, config: RunnableConfig) -> StateUpdate:
        progress = config.get("configurable", {}).get("progress")
        if isinstance(progress, PipelineProgress):
            progress.execution_id = state.get("execution_id")
        # Needed: the only local handler of the pipeline. It persists, never swallows.
        try:
            return await node(state)
        except Exception as exc:
            if step is not None and execution_log is not None:
                error = PipelineError.from_exception(step, exc)
                await execution_log.fail(state["execution_id"], to_outcome(state), error)
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
