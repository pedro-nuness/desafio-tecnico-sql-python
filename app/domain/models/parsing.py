"""Parser-agnostic intermediate representation (IR) of a PL/pgSQL routine.

Nothing here knows about pglast: any SQLParser adapter must translate its own AST
into these models.
"""

from collections.abc import Iterator
from enum import StrEnum

from app.domain.models.value_object import ValueObject


class RoutineKind(StrEnum):
    FUNCTION = "function"
    PROCEDURE = "procedure"


class ParameterMode(StrEnum):
    IN = "in"
    OUT = "out"
    INOUT = "inout"
    VARIADIC = "variadic"
    TABLE = "table"  # column of RETURNS TABLE(...)


class DeclarationKind(StrEnum):
    VARIABLE = "variable"
    RECORD = "record"
    CURSOR = "cursor"


class StatementKind(StrEnum):
    BLOCK = "block"
    ASSIGN = "assign"
    IF = "if"
    CASE = "case"
    LOOP = "loop"
    WHILE = "while"
    FOR_RANGE = "for_range"
    FOR_QUERY = "for_query"
    FOR_CURSOR = "for_cursor"
    FOR_DYNAMIC = "for_dynamic"
    FOREACH = "foreach"
    EXIT = "exit"
    CONTINUE = "continue"
    RETURN = "return"
    RETURN_NEXT = "return_next"
    RETURN_QUERY = "return_query"
    RAISE = "raise"
    ASSERT = "assert"
    SQL = "sql"
    DYNAMIC_SQL = "dynamic_sql"
    PERFORM = "perform"
    CALL = "call"
    GET_DIAGNOSTICS = "get_diagnostics"
    OPEN_CURSOR = "open_cursor"
    FETCH = "fetch"
    CLOSE_CURSOR = "close_cursor"
    COMMIT = "commit"
    ROLLBACK = "rollback"
    OTHER = "other"


class SqlCommand(StrEnum):
    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    MERGE = "merge"
    CALL = "call"
    OTHER = "other"


class Parameter(ValueObject):
    name: str
    data_type: str
    mode: ParameterMode
    has_default: bool = False


class SqlFragment(ValueObject):
    """An embedded SQL statement, analyzed with the SQL grammar (not the procedural one)."""

    text: str
    command: SqlCommand = SqlCommand.OTHER
    tables: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    cte_names: tuple[str, ...] = ()
    has_recursive_cte: bool = False
    locking_clauses: tuple[str, ...] = ()
    uses_jsonb: bool = False
    has_join: bool = False
    parse_error: str | None = None

    @property
    def has_cte(self) -> bool:
        return bool(self.cte_names)


class Declaration(ValueObject):
    name: str
    kind: DeclarationKind
    data_type: str | None = None
    line: int | None = None
    has_default: bool = False
    cursor_sql: SqlFragment | None = None


class ExceptionHandler(ValueObject):
    conditions: tuple[str, ...]
    body: tuple[Statement, ...] = ()


class Statement(ValueObject):
    kind: StatementKind
    line: int | None = None
    sql: SqlFragment | None = None
    expression: str | None = None
    target: str | None = None
    raise_level: str | None = None
    raise_message: str | None = None
    raise_condition: str | None = None
    diagnostics: tuple[str, ...] = ()
    into: bool = False
    body: tuple[Statement, ...] = ()
    else_body: tuple[Statement, ...] = ()
    exception_handlers: tuple[ExceptionHandler, ...] = ()

    def walk(self) -> Iterator[tuple[Statement, int]]:
        """Yield this statement and every nested one with its loop depth."""
        yield from _walk((self,), loop_depth=0)


LOOP_KINDS = frozenset(
    {
        StatementKind.LOOP,
        StatementKind.WHILE,
        StatementKind.FOR_RANGE,
        StatementKind.FOR_QUERY,
        StatementKind.FOR_CURSOR,
        StatementKind.FOR_DYNAMIC,
        StatementKind.FOREACH,
    }
)


def _walk(statements: tuple[Statement, ...], loop_depth: int) -> Iterator[tuple[Statement, int]]:
    for statement in statements:
        yield statement, loop_depth
        inner_depth = loop_depth + 1 if statement.kind in LOOP_KINDS else loop_depth
        yield from _walk(statement.body, inner_depth)
        yield from _walk(statement.else_body, inner_depth)
        for handler in statement.exception_handlers:
            yield from _walk(handler.body, loop_depth)


class ParsedProcedure(ValueObject):
    name: str
    schema_name: str | None = None
    kind: RoutineKind
    language: str
    parameters: tuple[Parameter, ...] = ()
    return_type: str | None = None
    returns_set: bool = False
    declarations: tuple[Declaration, ...] = ()
    body: tuple[Statement, ...] = ()
    referenced_tables: tuple[str, ...] = ()
    called_functions: tuple[str, ...] = ()
    parser: str
    warnings: tuple[str, ...] = ()

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.name}" if self.schema_name else self.name

    def iter_statements(self) -> Iterator[tuple[Statement, int]]:
        """Depth-first traversal of the body yielding (statement, loop_depth)."""
        yield from _walk(self.body, loop_depth=0)

    def sql_fragments(self) -> Iterator[SqlFragment]:
        """Every embedded SQL statement: cursor declarations and body statements."""
        for declaration in self.declarations:
            if declaration.cursor_sql is not None:
                yield declaration.cursor_sql
        for statement, _ in self.iter_statements():
            if statement.sql is not None:
                yield statement.sql


ExceptionHandler.model_rebuild()
