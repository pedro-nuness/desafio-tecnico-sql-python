"""SQLParser adapter backed by pglast (libpg_query, the real PostgreSQL parser).

Two grammars are combined, all AST-based:

1. `pglast.parse_sql` -> CreateFunctionStmt: name, kind, parameters/modes, return type.
2. `pglast.parse_plpgsql` -> PL/pgSQL procedural tree (blocks, loops, IF, RAISE,
   GET DIAGNOSTICS, EXCEPTION, COMMIT...). It is the JSON produced by libpg_query's
   port of the PL/pgSQL compiler.
3. Every embedded SQL expression found in (2) is plain text; it is parsed again with
   `parse_sql` and walked with a pglast Visitor (tables, functions, CTEs, locking, JSONB).

Tokens (`pglast.scan`) are used only to split `target := expression` assignments.
No regular expressions are used.

Known limitation: libpg_query cannot resolve parameter %TYPE without a catalog.
Parameters with %TYPE are rewritten before compilation; native parsing errors propagate.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import pglast
from pglast import ast, enums, visitors
from pglast.parser import ParseError
from pglast.stream import RawStream

from app.features.modernization.domain.models.parsing import (
    Declaration,
    DeclarationKind,
    ExceptionHandler,
    Parameter,
    ParameterMode,
    ParsedProcedure,
    RoutineKind,
    SqlCommand,
    SqlFragment,
    Statement,
    StatementKind,
)
from app.shared.errors import DomainError

PARSER_NAME = "pglast"

# libpg_query renders JSON; these aliases document the shape we consume.
type JsonValue = str | int | float | bool | JsonObject | list[JsonValue] | None
type JsonObject = dict[str, JsonValue]

_MODES: Mapping[enums.FunctionParameterMode, ParameterMode] = {
    enums.FunctionParameterMode.FUNC_PARAM_IN: ParameterMode.IN,
    enums.FunctionParameterMode.FUNC_PARAM_DEFAULT: ParameterMode.IN,
    enums.FunctionParameterMode.FUNC_PARAM_OUT: ParameterMode.OUT,
    enums.FunctionParameterMode.FUNC_PARAM_INOUT: ParameterMode.INOUT,
    enums.FunctionParameterMode.FUNC_PARAM_VARIADIC: ParameterMode.VARIADIC,
    enums.FunctionParameterMode.FUNC_PARAM_TABLE: ParameterMode.TABLE,
}

# PostgreSQL elog.h levels as emitted by the PL/pgSQL compiler.
_ELOG_LEVELS: Mapping[int, str] = {
    10: "DEBUG", 11: "DEBUG", 12: "DEBUG", 13: "DEBUG", 14: "DEBUG",
    15: "LOG", 17: "INFO", 18: "NOTICE", 19: "WARNING", 21: "EXCEPTION",
}  # fmt: skip

_JSON_OPERATORS = frozenset({"->", "->>", "#>", "#>>", "@>", "<@", "?", "?|", "?&", "#-", "@?"})
_IMPLICIT_DATUMS = frozenset({"found", "sqlstate", "sqlerrm"})
_TYPE_PLACEHOLDER = "text"


class PglastParser:
    def parse(self, source_code: str) -> ParsedProcedure:
        # Needed: libpg_query rejecting the source is the caller's input error.
        try:
            return self._parse(source_code)
        except ParseError as exc:
            raise DomainError(f"Invalid SQL or PL/pgSQL: {exc}") from exc

    def _parse(self, source_code: str) -> ParsedProcedure:
        statement_sql, create = _find_create_function(source_code)
        language = _language(create)
        if language != "plpgsql":
            raise DomainError(
                f"Only LANGUAGE plpgsql is supported (got {language!r})", language=language
            )

        warnings: list[str] = []
        tree = _parse_plpgsql(statement_sql, create, warnings)
        builder = _BodyBuilder(
            warnings, line_offset=_body_line_offset(source_code, statement_sql, create)
        )
        parameters = _parameters(create)
        parameter_names = {p.name for p in parameters}
        declarations = builder.declarations(tree.get("datums"), parameter_names)
        body = builder.block_body(tree.get("action"))
        schema_name, name = _qualified_name(create.funcname)
        return_type, returns_set = _return_type(create)

        return ParsedProcedure(
            name=name,
            schema_name=schema_name,
            kind=RoutineKind.PROCEDURE if create.is_procedure else RoutineKind.FUNCTION,
            language=language,
            parameters=parameters,
            return_type=return_type,
            returns_set=returns_set,
            declarations=declarations,
            body=body,
            referenced_tables=tuple(sorted(builder.tables)),
            called_functions=tuple(sorted(builder.functions)),
            parser=PARSER_NAME,
            warnings=tuple(warnings),
        )


# --------------------------------------------------------------------------- header


def _find_create_function(source_code: str) -> tuple[str, ast.CreateFunctionStmt]:
    statements = pglast.split(source_code)
    for statement_sql in statements:
        raw = pglast.parse_sql(statement_sql)[0].stmt
        if isinstance(raw, ast.CreateFunctionStmt):
            return statement_sql, raw
    raise DomainError("No CREATE FUNCTION / CREATE PROCEDURE statement found")


def _body_line_offset(source_code: str, statement_sql: str, create: ast.CreateFunctionStmt) -> int:
    """Lines before the body start (body line 1 == the line holding the opening quote)."""
    body = next(
        (
            option.arg[0].sval
            for option in create.options or ()
            if option.defname == "as" and isinstance(option.arg, tuple) and option.arg
        ),
        None,
    )
    statement_start = source_code.find(statement_sql)
    body_start = statement_sql.find(body) if body else -1
    if statement_start < 0 or body_start < 0:
        return 0
    return source_code[: statement_start + body_start].count("\n")


def _language(create: ast.CreateFunctionStmt) -> str:
    for option in create.options or ():
        if option.defname == "language" and isinstance(option.arg, ast.String):
            return option.arg.sval.lower()
    return "sql"


def _qualified_name(names: Sequence[ast.String]) -> tuple[str | None, str]:
    parts = [part.sval for part in names]
    return (parts[-2] if len(parts) > 1 else None), parts[-1]


def _type_name(type_name: ast.TypeName) -> str:
    rendered = RawStream()(type_name)
    return rendered.removeprefix("pg_catalog.")


def _parameters(create: ast.CreateFunctionStmt) -> tuple[Parameter, ...]:
    return tuple(
        Parameter(
            name=parameter.name or f"${index}",
            data_type=_type_name(parameter.argType),
            mode=_MODES[parameter.mode],
            has_default=parameter.defexpr is not None,
        )
        for index, parameter in enumerate(create.parameters or (), start=1)
    )


def _return_type(create: ast.CreateFunctionStmt) -> tuple[str | None, bool]:
    if create.returnType is None:
        return None, False
    table_columns = [
        p for p in create.parameters or () if p.mode is enums.FunctionParameterMode.FUNC_PARAM_TABLE
    ]
    if table_columns:
        columns = ", ".join(f"{p.name} {_type_name(p.argType)}" for p in table_columns)
        return f"TABLE({columns})", True
    returns_set = bool(create.returnType.setof)
    return _type_name(create.returnType).removeprefix("SETOF "), returns_set


# --------------------------------------------------------------------------- PL/pgSQL tree


def _parse_plpgsql(
    statement_sql: str, create: ast.CreateFunctionStmt, warnings: list[str]
) -> JsonObject:
    tree = (
        _parse_with_placeholder_types(create, warnings)
        if _has_pct_type_parameters(create)
        else pglast.parse_plpgsql(statement_sql)
    )
    function = tree[0].get("PLpgSQL_function") if tree else None
    if not isinstance(function, dict):
        raise DomainError("pglast returned no PL/pgSQL function tree")
    return function


def _has_pct_type_parameters(create: ast.CreateFunctionStmt) -> bool:
    return any(p.argType.pct_type for p in create.parameters or ())


def _parse_with_placeholder_types(
    create: ast.CreateFunctionStmt, warnings: list[str]
) -> list[JsonObject]:
    rewritten = []
    for parameter in create.parameters or ():
        if parameter.argType.pct_type:
            warnings.append(
                f"Parameter {parameter.name!r} uses %TYPE ({_type_name(parameter.argType)}); "
                "the body was parsed with a placeholder type (no catalog available)."
            )
            parameter = ast.FunctionParameter(
                name=parameter.name,
                argType=ast.TypeName(names=(ast.String(sval=_TYPE_PLACEHOLDER),), typemod=-1),
                mode=parameter.mode,
                defexpr=parameter.defexpr,
            )
        rewritten.append(parameter)
    clone = ast.CreateFunctionStmt(
        is_procedure=create.is_procedure,
        replace=create.replace,
        funcname=create.funcname,
        parameters=tuple(rewritten),
        returnType=create.returnType,
        options=create.options,
        sql_body=create.sql_body,
    )
    return pglast.parse_plpgsql(RawStream()(clone) + ";")


def _obj(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _unwrap(node: JsonValue) -> tuple[str, JsonObject]:
    """{"PLpgSQL_stmt_if": {...}} -> ("PLpgSQL_stmt_if", {...})."""
    obj = _obj(node)
    if len(obj) != 1:
        return "", {}
    ((tag, payload),) = obj.items()
    return tag, _obj(payload)


def _nodes(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _expr_text(value: JsonValue) -> str | None:
    _, payload = _unwrap(value)
    query = payload.get("query")
    return query if isinstance(query, str) else None


def _int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


@dataclass
class _BodyBuilder:
    warnings: list[str]
    line_offset: int = 0
    """PL/pgSQL line numbers are relative to the body; offset makes them source-absolute."""
    tables: set[str] = field(default_factory=set)
    functions: set[str] = field(default_factory=set)
    _datum_names: dict[int, str] = field(default_factory=dict)

    # ---- declarations

    def declarations(self, datums: JsonValue, parameter_names: set[str]) -> tuple[Declaration, ...]:
        result: list[Declaration] = []
        for index, datum in enumerate(_nodes(datums)):
            tag, payload = _unwrap(datum)
            name = _str(payload.get("refname"))
            if name:
                self._datum_names[index] = name
            line = _int(payload.get("lineno"))
            if (
                not name
                or name in parameter_names
                or name in _IMPLICIT_DATUMS
                or name.startswith(("(", "__"))  # unnamed rows, compiler-generated CASE vars
                or line is None
                or line < 0
            ):
                continue
            result.append(self._declaration(tag, payload, name, line + self.line_offset))
        return tuple(result)

    def _declaration(self, tag: str, payload: JsonObject, name: str, line: int) -> Declaration:
        if tag == "PLpgSQL_rec":
            return Declaration(
                name=name, kind=DeclarationKind.RECORD, data_type="record", line=line
            )
        _, datatype = _unwrap(payload.get("datatype"))
        data_type = _str(datatype.get("typname"))
        cursor_query = _expr_text(payload.get("cursor_explicit_expr"))
        if cursor_query is not None or data_type == "refcursor":
            return Declaration(
                name=name,
                kind=DeclarationKind.CURSOR,
                data_type=data_type,
                line=line,
                cursor_sql=self._sql(cursor_query) if cursor_query else None,
            )
        return Declaration(
            name=name,
            kind=DeclarationKind.VARIABLE,
            data_type=data_type,
            line=line,
            has_default="default_val" in payload,
        )

    # ---- statements

    def block_body(self, action: JsonValue) -> tuple[Statement, ...]:
        tag, payload = _unwrap(action)
        if tag != "PLpgSQL_stmt_block":
            raise DomainError("PL/pgSQL function has no top-level block")
        block = self._block(payload)
        body = block.body if not block.exception_handlers else (block,)
        return _drop_implicit_return(body)

    def statements(self, nodes: JsonValue) -> tuple[Statement, ...]:
        return tuple(self.statement(node) for node in _nodes(nodes))

    def statement(self, node: JsonValue) -> Statement:
        tag, payload = _unwrap(node)
        handler = _HANDLERS.get(tag)
        if handler is None:
            self.warnings.append(f"Unsupported PL/pgSQL node {tag!r}; kept as OTHER.")
            return Statement(kind=StatementKind.OTHER, line=self._line(payload))
        return handler(self, payload)

    def _block(self, payload: JsonObject) -> Statement:
        handlers = tuple(
            ExceptionHandler(
                conditions=tuple(
                    _str(_unwrap(condition)[1].get("condname")) or "?"
                    for condition in _nodes(exception.get("conditions"))
                ),
                body=self.statements(exception.get("action")),
            )
            for exception in (
                _unwrap(item)[1]
                for item in _nodes(_unwrap(payload.get("exceptions"))[1].get("exc_list"))
            )
        )
        return Statement(
            kind=StatementKind.BLOCK,
            line=self._line(payload),
            body=self.statements(payload.get("body")),
            exception_handlers=handlers,
        )

    def _simple(self, kind: StatementKind) -> Callable[[JsonObject], Statement]:
        return lambda payload: Statement(kind=kind, line=self._line(payload))

    def _sql_statement(self, kind: StatementKind, key: str) -> Callable[[JsonObject], Statement]:
        def build(payload: JsonObject) -> Statement:
            text = _expr_text(payload.get(key)) or ""
            return Statement(
                kind=kind,
                line=self._line(payload),
                sql=self._sql(text),
                into=payload.get("into") is True,
                target=self._target(payload.get("target")),
                body=self.statements(payload.get("body")),
            )

        return build

    def _expression_statement(
        self, kind: StatementKind, key: str
    ) -> Callable[[JsonObject], Statement]:
        def build(payload: JsonObject) -> Statement:
            expression = _expr_text(payload.get(key))
            if expression:
                self._scan_expression(expression)
            return Statement(
                kind=kind,
                line=self._line(payload),
                expression=expression,
                body=self.statements(payload.get("body")),
            )

        return build

    def _assign(self, payload: JsonObject) -> Statement:
        text = _expr_text(payload.get("expr")) or ""
        target, expression = _split_assignment(text)
        self._scan_expression(expression)
        return Statement(
            kind=StatementKind.ASSIGN,
            line=self._line(payload),
            target=target or self._datum_names.get(_int(payload.get("varno")) or -1),
            expression=expression,
        )

    def _if(self, payload: JsonObject) -> Statement:
        condition = _expr_text(payload.get("cond"))
        if condition:
            self._scan_expression(condition)
        else_body = self.statements(payload.get("else_body"))
        for elsif in reversed(_nodes(payload.get("elsif_list"))):
            _, branch = _unwrap(elsif)
            branch_condition = _expr_text(branch.get("cond"))
            if branch_condition:
                self._scan_expression(branch_condition)
            else_body = (
                Statement(
                    kind=StatementKind.IF,
                    line=self._line(branch),
                    expression=branch_condition,
                    body=self.statements(branch.get("stmts")),
                    else_body=else_body,
                ),
            )
        return Statement(
            kind=StatementKind.IF,
            line=self._line(payload),
            expression=condition,
            body=self.statements(payload.get("then_body")),
            else_body=else_body,
        )

    def _case(self, payload: JsonObject) -> Statement:
        branches = tuple(
            Statement(
                kind=StatementKind.IF,
                line=self._line(branch),
                expression=_expr_text(branch.get("expr")),
                body=self.statements(branch.get("stmts")),
            )
            for branch in (_unwrap(when)[1] for when in _nodes(payload.get("case_when_list")))
        )
        return Statement(
            kind=StatementKind.CASE,
            line=self._line(payload),
            expression=_expr_text(payload.get("t_expr")),
            body=branches,
            else_body=self.statements(payload.get("else_stmts")),
        )

    def _for_range(self, payload: JsonObject) -> Statement:
        lower, upper = _expr_text(payload.get("lower")), _expr_text(payload.get("upper"))
        return Statement(
            kind=StatementKind.FOR_RANGE,
            line=self._line(payload),
            target=self._target(payload.get("var")),
            expression=f"{lower}..{upper}",
            body=self.statements(payload.get("body")),
        )

    def _for_cursor(self, payload: JsonObject) -> Statement:
        return Statement(
            kind=StatementKind.FOR_CURSOR,
            line=self._line(payload),
            target=self._target(payload.get("var")),
            expression=self._datum_names.get(_int(payload.get("curvar")) or -1),
            body=self.statements(payload.get("body")),
        )

    def _foreach(self, payload: JsonObject) -> Statement:
        return Statement(
            kind=StatementKind.FOREACH,
            line=self._line(payload),
            target=self._datum_names.get(_int(payload.get("varno")) or -1),
            expression=_expr_text(payload.get("expr")),
            body=self.statements(payload.get("body")),
        )

    def _exit(self, payload: JsonObject) -> Statement:
        return Statement(
            kind=StatementKind.EXIT if payload.get("is_exit") is True else StatementKind.CONTINUE,
            line=self._line(payload),
            expression=_expr_text(payload.get("cond")),
        )

    def _return(self, payload: JsonObject) -> Statement:
        return Statement(
            kind=StatementKind.RETURN,
            line=self._line(payload),
            expression=_expr_text(payload.get("expr")),
        )

    def _raise(self, payload: JsonObject) -> Statement:
        level = _ELOG_LEVELS.get(_int(payload.get("elog_level")) or 21, "EXCEPTION")
        message = _str(payload.get("message"))
        for option in _nodes(payload.get("options")):
            _, raise_option = _unwrap(option)
            if raise_option.get("opt_type") == 1:  # PLPGSQL_RAISEOPTION_MESSAGE
                message = message or _expr_text(raise_option.get("expr"))
        return Statement(
            kind=StatementKind.RAISE,
            line=self._line(payload),
            raise_level=level,
            raise_message=message,
            raise_condition=_str(payload.get("condname")),
        )

    def _get_diagnostics(self, payload: JsonObject) -> Statement:
        items = tuple(
            f"{self._datum_names.get(_int(item.get('target')) or -1, '?')} = {item.get('kind')}"
            for item in (_unwrap(node)[1] for node in _nodes(payload.get("diag_items")))
        )
        return Statement(
            kind=StatementKind.GET_DIAGNOSTICS,
            line=self._line(payload),
            diagnostics=items,
        )

    def _dynamic(self, kind: StatementKind) -> Callable[[JsonObject], Statement]:
        def build(payload: JsonObject) -> Statement:
            return Statement(
                kind=kind,
                line=self._line(payload),
                expression=_expr_text(payload.get("query")),
                into=payload.get("into") is True,
                target=self._target(payload.get("target") or payload.get("var")),
                body=self.statements(payload.get("body")),
            )

        return build

    def _cursor_op(self, kind: StatementKind) -> Callable[[JsonObject], Statement]:
        def build(payload: JsonObject) -> Statement:
            query = _expr_text(payload.get("query"))
            return Statement(
                kind=kind,
                line=self._line(payload),
                target=self._datum_names.get(_int(payload.get("curvar")) or -1),
                sql=self._sql(query) if query else None,
            )

        return build

    # ---- helpers

    def _line(self, payload: JsonObject) -> int | None:
        line = _int(payload.get("lineno"))
        return line + self.line_offset if line is not None and line > 0 else None

    def _target(self, value: JsonValue) -> str | None:
        tag, payload = _unwrap(value)
        if tag == "PLpgSQL_row":
            fields = [_str(_obj(f).get("name")) for f in _nodes(payload.get("fields"))]
            return ", ".join(name for name in fields if name) or None
        return _str(payload.get("refname"))

    def _sql(self, text: str) -> SqlFragment:
        fragment = analyze_sql(text)
        self.tables.update(fragment.tables)
        self.functions.update(fragment.functions)
        if fragment.parse_error:
            self.warnings.append(f"Could not parse embedded SQL {text!r}: {fragment.parse_error}")
        return fragment

    def _scan_expression(self, expression: str) -> None:
        """Expressions (conditions, assignments) may call functions or read tables."""
        fragment = analyze_sql(f"SELECT {expression}")
        if fragment.parse_error is None:
            self.tables.update(fragment.tables)
            self.functions.update(fragment.functions)


_HANDLERS: Mapping[str, Callable[[_BodyBuilder, JsonObject], Statement]] = {
    "PLpgSQL_stmt_block": lambda b, p: b._block(p),
    "PLpgSQL_stmt_assign": lambda b, p: b._assign(p),
    "PLpgSQL_stmt_if": lambda b, p: b._if(p),
    "PLpgSQL_stmt_case": lambda b, p: b._case(p),
    "PLpgSQL_stmt_loop": lambda b, p: b._expression_statement(StatementKind.LOOP, "cond")(p),
    "PLpgSQL_stmt_while": lambda b, p: b._expression_statement(StatementKind.WHILE, "cond")(p),
    "PLpgSQL_stmt_fori": lambda b, p: b._for_range(p),
    "PLpgSQL_stmt_fors": lambda b, p: b._sql_statement(StatementKind.FOR_QUERY, "query")(p),
    "PLpgSQL_stmt_forc": lambda b, p: b._for_cursor(p),
    "PLpgSQL_stmt_dynfors": lambda b, p: b._dynamic(StatementKind.FOR_DYNAMIC)(p),
    "PLpgSQL_stmt_foreach_a": lambda b, p: b._foreach(p),
    "PLpgSQL_stmt_exit": lambda b, p: b._exit(p),
    "PLpgSQL_stmt_return": lambda b, p: b._return(p),
    "PLpgSQL_stmt_return_next": lambda b, p: b._expression_statement(
        StatementKind.RETURN_NEXT, "expr"
    )(p),
    "PLpgSQL_stmt_return_query": lambda b, p: (
        b._sql_statement(StatementKind.RETURN_QUERY, "query")(p)
        if "query" in p
        else b._dynamic(StatementKind.RETURN_QUERY)(p | {"query": p.get("dynquery")})
    ),
    "PLpgSQL_stmt_raise": lambda b, p: b._raise(p),
    "PLpgSQL_stmt_assert": lambda b, p: b._expression_statement(StatementKind.ASSERT, "cond")(p),
    "PLpgSQL_stmt_execsql": lambda b, p: b._sql_statement(StatementKind.SQL, "sqlstmt")(p),
    "PLpgSQL_stmt_dynexecute": lambda b, p: b._dynamic(StatementKind.DYNAMIC_SQL)(p),
    "PLpgSQL_stmt_perform": lambda b, p: b._sql_statement(StatementKind.PERFORM, "expr")(p),
    "PLpgSQL_stmt_call": lambda b, p: b._sql_statement(StatementKind.CALL, "expr")(p),
    "PLpgSQL_stmt_getdiag": lambda b, p: b._get_diagnostics(p),
    "PLpgSQL_stmt_open": lambda b, p: b._cursor_op(StatementKind.OPEN_CURSOR)(p),
    "PLpgSQL_stmt_fetch": lambda b, p: b._cursor_op(StatementKind.FETCH)(p),
    "PLpgSQL_stmt_close": lambda b, p: b._cursor_op(StatementKind.CLOSE_CURSOR)(p),
    "PLpgSQL_stmt_commit": lambda b, p: b._simple(StatementKind.COMMIT)(p),
    "PLpgSQL_stmt_rollback": lambda b, p: b._simple(StatementKind.ROLLBACK)(p),
}


def _drop_implicit_return(body: tuple[Statement, ...]) -> tuple[Statement, ...]:
    """The PL/pgSQL compiler appends a RETURN without line number; it is not user code."""
    if body and body[-1].kind is StatementKind.RETURN and body[-1].line is None:
        return body[:-1]
    return body


def _split_assignment(text: str) -> tuple[str | None, str]:
    """Split `target := expr` using the SQL lexer (offsets are byte-based)."""
    encoded = text.encode()
    tokens = pglast.scan(text)
    for token in tokens:
        if token.name == "COLON_EQUALS" or (token.name == "ASCII_61" and token.start > 0):
            target = encoded[: token.start].decode().strip()
            return target or None, encoded[token.end + 1 :].decode().strip()
    return None, text


# --------------------------------------------------------------------------- embedded SQL

_COMMANDS: Mapping[type[ast.Node], SqlCommand] = {
    ast.SelectStmt: SqlCommand.SELECT,
    ast.InsertStmt: SqlCommand.INSERT,
    ast.UpdateStmt: SqlCommand.UPDATE,
    ast.DeleteStmt: SqlCommand.DELETE,
    ast.MergeStmt: SqlCommand.MERGE,
    ast.CallStmt: SqlCommand.CALL,
}


class _SqlVisitor(visitors.Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.tables: set[str] = set()
        self.functions: set[str] = set()
        self.cte_names: list[str] = []
        self.recursive = False
        self.locking: list[str] = []
        self.jsonb = False
        self.join = False

    def visit_RangeVar(self, ancestors: visitors.Ancestor, node: ast.RangeVar) -> None:
        name = f"{node.schemaname}.{node.relname}" if node.schemaname else node.relname
        self.tables.add(name)

    def visit_FuncCall(self, ancestors: visitors.Ancestor, node: ast.FuncCall) -> None:
        name = ".".join(part.sval for part in node.funcname)
        self.functions.add(name)
        if name.rpartition(".")[2].lower().startswith("jsonb"):
            self.jsonb = True

    def visit_WithClause(self, ancestors: visitors.Ancestor, node: ast.WithClause) -> None:
        self.recursive = self.recursive or bool(node.recursive)

    def visit_CommonTableExpr(
        self, ancestors: visitors.Ancestor, node: ast.CommonTableExpr
    ) -> None:
        self.cte_names.append(node.ctename)

    def visit_LockingClause(self, ancestors: visitors.Ancestor, node: ast.LockingClause) -> None:
        self.locking.append(_LOCK_NAMES.get(node.strength, str(node.strength)))

    def visit_JoinExpr(self, ancestors: visitors.Ancestor, node: ast.JoinExpr) -> None:
        self.join = True

    def visit_A_Expr(self, ancestors: visitors.Ancestor, node: ast.A_Expr) -> None:
        operators = {part.sval for part in node.name or () if isinstance(part, ast.String)}
        if operators & _JSON_OPERATORS:
            self.jsonb = True

    def visit_TypeName(self, ancestors: visitors.Ancestor, node: ast.TypeName) -> None:
        if any(isinstance(p, ast.String) and p.sval == "jsonb" for p in node.names or ()):
            self.jsonb = True


_LOCK_NAMES: Mapping[enums.LockClauseStrength, str] = {
    enums.LockClauseStrength.LCS_FORUPDATE: "FOR UPDATE",
    enums.LockClauseStrength.LCS_FORNOKEYUPDATE: "FOR NO KEY UPDATE",
    enums.LockClauseStrength.LCS_FORSHARE: "FOR SHARE",
    enums.LockClauseStrength.LCS_FORKEYSHARE: "FOR KEY SHARE",
}


def analyze_sql(text: str) -> SqlFragment:
    # Needed: embedded-SQL analysis is best effort; an unparsable fragment becomes a
    # warning / UNPARSED fact (SqlFragment.parse_error) instead of aborting the whole run.
    try:
        statements = pglast.parse_sql(text)
    except ParseError as exc:
        return SqlFragment(text=text, parse_error=str(exc))
    visitor = _SqlVisitor()
    visitor(statements)
    first = statements[0].stmt if statements else None
    cte_names = tuple(dict.fromkeys(visitor.cte_names))
    has_many_from_items = _has_many_from_items(first)
    return SqlFragment(
        text=text,
        command=_COMMANDS.get(type(first), SqlCommand.OTHER) if first else SqlCommand.OTHER,
        tables=tuple(sorted(visitor.tables - set(cte_names))),
        functions=tuple(sorted(visitor.functions)),
        cte_names=cte_names,
        has_recursive_cte=visitor.recursive,
        locking_clauses=tuple(dict.fromkeys(visitor.locking)),
        uses_jsonb=visitor.jsonb,
        has_join=visitor.join or has_many_from_items,
    )


def _has_many_from_items(node: ast.Node | None) -> bool:
    from_clause: Iterable[ast.Node] | None = getattr(node, "fromClause", None)
    return len(tuple(from_clause or ())) > 1
