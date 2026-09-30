from typing import Protocol

from app.domain.models.parsing import ParsedProcedure


class SQLParser(Protocol):
    """Turns routine source code into the parser-agnostic ParsedProcedure IR.

    Implementations must raise app.domain.exceptions.ParsingError when the source
    cannot be represented, and must not leak library-specific AST objects.
    """

    def parse(self, source_code: str) -> ParsedProcedure: ...
