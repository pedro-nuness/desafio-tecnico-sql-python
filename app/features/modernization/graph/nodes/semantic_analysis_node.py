from app.features.modernization.domain.enums import PipelineStep
from app.features.modernization.domain.exceptions import ModernizationError
from app.features.modernization.domain.services.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.graph.state import ModernizationState, StateUpdate


class SemanticAnalysisNode:
    """Deterministic analysis of the parsed IR: features, risks, strategy."""

    def __init__(self, analyzer: SemanticAnalyzer) -> None:
        self._analyzer = analyzer

    def __call__(self, state: ModernizationState) -> StateUpdate:
        procedure = state.get("parsed_procedure")
        if procedure is None:
            raise ModernizationError("no parsed procedure in state")
        analysis = self._analyzer.analyze(procedure)
        warnings = (
            []
            if state.get("schema_context")
            else ["No schema provided: column types and constraints are inferred by the LLM."]
        )
        return StateUpdate(
            semantic_analysis=analysis,
            warnings=warnings,
            completed_steps=[PipelineStep.SEMANTIC_ANALYSIS],
        )
