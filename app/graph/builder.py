from collections.abc import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.application.ports.parsing.sql_parser import SQLParser
from app.application.ports.validation.code_validator import CodeValidator
from app.application.services.code_generation_service import CodeGenerationService
from app.domain.enums import ModernizationStatus, PipelineStep
from app.domain.services.semantic_analyzer import SemanticAnalyzer
from app.graph.nodes.generation_node import GenerationNode
from app.graph.nodes.parsing_node import ParsingNode
from app.graph.nodes.semantic_analysis_node import SemanticAnalysisNode
from app.graph.nodes.validation_node import ValidationNode
from app.graph.state import ModernizationState

type ModernizationGraph = CompiledStateGraph[ModernizationState]

GRAPH_NAME = "modernization"


def build_modernization_graph(
    *,
    parser: SQLParser,
    analyzer: SemanticAnalyzer,
    generation_service: CodeGenerationService,
    validator: CodeValidator,
) -> ModernizationGraph:
    """START -> parsing -> semantic_analysis -> generation -> validation -> END.

    A failed step short-circuits to END so later steps never run on missing input.
    Dependencies are injected into node instances (closures), keeping nodes thin.
    """
    graph = StateGraph(ModernizationState)
    graph.add_node(PipelineStep.PARSING.value, ParsingNode(parser))
    graph.add_node(PipelineStep.SEMANTIC_ANALYSIS.value, SemanticAnalysisNode(analyzer))
    graph.add_node(PipelineStep.GENERATION.value, GenerationNode(generation_service))
    graph.add_node(PipelineStep.VALIDATION.value, ValidationNode(validator))

    graph.add_edge(START, PipelineStep.PARSING.value)
    _continue_unless_failed(graph, PipelineStep.PARSING, PipelineStep.SEMANTIC_ANALYSIS)
    _continue_unless_failed(graph, PipelineStep.SEMANTIC_ANALYSIS, PipelineStep.GENERATION)
    _continue_unless_failed(graph, PipelineStep.GENERATION, PipelineStep.VALIDATION)
    graph.add_edge(PipelineStep.VALIDATION.value, END)
    return graph.compile(name=GRAPH_NAME)


def _continue_unless_failed(
    graph: StateGraph[ModernizationState], source: PipelineStep, target: PipelineStep
) -> None:
    graph.add_conditional_edges(source.value, _route_to(target.value), [target.value, END])


def _route_to(target: str) -> Callable[[ModernizationState], str]:
    def route(state: ModernizationState) -> str:
        # .get: runs started from LangGraph Studio/API may send only the input fields.
        return END if state.get("status") is ModernizationStatus.FAILURE else target

    return route
