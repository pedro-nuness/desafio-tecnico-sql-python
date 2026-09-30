from app.application.ports.parsing.sql_parser import SQLParser
from app.domain.enums import PipelineStep
from app.domain.exceptions import ParsingError
from app.graph.state import ModernizationState, StateUpdate, failed


class ParsingNode:
    """Deterministic parsing: source code -> ParsedProcedure (via the SQLParser port)."""

    def __init__(self, parser: SQLParser) -> None:
        self._parser = parser

    def __call__(self, state: ModernizationState) -> StateUpdate:
        try:
            procedure = self._parser.parse(state["source_code"])
        except ParsingError as exc:
            return failed(PipelineStep.PARSING, exc)
        return StateUpdate(parsed_procedure=procedure, completed_steps=[PipelineStep.PARSING])
