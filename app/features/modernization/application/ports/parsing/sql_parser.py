from typing import Protocol

from app.features.modernization.domain.models.parsing import ParsedProcedure


class SQLParser(Protocol):
    """Turns routine source code into the parser-agnostic ParsedProcedure IR.

    Native parser exceptions propagate. Returned IR never contains library-specific AST objects.
    """

    def parse(self, source_code: str) -> ParsedProcedure: ...
