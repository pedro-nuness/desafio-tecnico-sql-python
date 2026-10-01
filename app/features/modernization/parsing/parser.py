from typing import Protocol

from app.features.modernization.parsing.domain import ParsedProcedure


class SQLParser(Protocol):
    """Parsing strategy, one per source dialect (plpgsql.py today; T-SQL or PL/SQL would be
    siblings). Turns routine source code into the parser-agnostic ParsedProcedure IR.

    Invalid input raises DomainError. Returned IR never contains library-specific AST objects.
    """

    def parse(self, source_code: str) -> ParsedProcedure: ...
