from app.features.modernization.application.ports.parsing.sql_parser import SQLParser
from app.features.modernization.domain.enums import PipelineStep
from app.features.modernization.graph.state import ModernizationState, StateUpdate


class ParsingNode:
    """Deterministic parsing: source code -> ParsedProcedure (via the SQLParser port)."""

    def __init__(self, parser: SQLParser) -> None:
        self._parser = parser

    def __call__(self, state: ModernizationState) -> StateUpdate:
        procedure = self._parser.parse(state["source_code"])
        return StateUpdate(parsed_procedure=procedure, completed_steps=[PipelineStep.PARSING])
