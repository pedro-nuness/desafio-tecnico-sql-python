"""Risks the Python version must handle, and the recommendations they (and the constructs) lead to.

Texts and severities live in catalog.py; this module only decides which ones apply.
"""

from collections.abc import Container, Iterator

from app.features.modernization.analysis.catalog import (
    DATABASE_ROUND_TRIP_KINDS,
    RECOMMENDATIONS,
    RISKS,
    is_builtin_function,
)
from app.features.modernization.analysis.domain import Recommendation, SemanticRisk, SqlConstruct
from app.features.modernization.parsing.domain import (
    LOOP_KINDS,
    ParsedProcedure,
    SqlFragment,
    Statement,
    StatementKind,
)


def is_dynamic_sql(statement: Statement) -> bool:
    if statement.kind in {StatementKind.DYNAMIC_SQL, StatementKind.FOR_DYNAMIC}:
        return True
    # RETURN QUERY EXECUTE: the query is an expression, not parsed SQL
    return statement.kind is StatementKind.RETURN_QUERY and statement.sql is None


def fragments_with_lines(procedure: ParsedProcedure) -> Iterator[tuple[SqlFragment, int | None]]:
    for declaration in procedure.declarations:
        if declaration.cursor_sql is not None:
            yield declaration.cursor_sql, declaration.line
    for statement, _ in procedure.iter_statements():
        if statement.sql is not None:
            yield statement.sql, statement.line


def detect_risks(
    procedure: ParsedProcedure, constructs: Container[SqlConstruct]
) -> tuple[SemanticRisk, ...]:
    risks: list[SemanticRisk] = []
    for statement, loop_depth in procedure.iter_statements():
        if statement.kind in LOOP_KINDS and loop_depth == 0:
            risks.extend(_n_plus_one(statement))
        if is_dynamic_sql(statement):
            risks.append(_risk("DYNAMIC_SQL", statement.line))
        if statement.kind in {StatementKind.COMMIT, StatementKind.ROLLBACK}:
            risks.append(
                _risk("TRANSACTION_CONTROL", statement.line, command=statement.kind.value.upper())
            )
        for handler in statement.exception_handlers:
            # Only RAISE EXCEPTION (or a bare RAISE;) propagates; NOTICE/WARNING just log.
            reraises = any(
                inner.kind is StatementKind.RAISE and inner.raise_level == "EXCEPTION"
                for inner, _ in _walk_all(handler.body)
            )
            if "others" in (c.lower() for c in handler.conditions) and not reraises:
                risks.append(_risk("SWALLOWED_EXCEPTION", statement.line))
    for fragment, line in fragments_with_lines(procedure):
        if fragment.parse_error:
            risks.append(_risk("UNPARSED_SQL", line, error=fragment.parse_error))
    if SqlConstruct.ROW_LOCKING in constructs:
        risks.append(_risk("ROW_LOCKING"))
    if SqlConstruct.GET_DIAGNOSTICS in constructs:
        risks.append(_risk("DIAGNOSTICS_SEMANTICS"))
    if SqlConstruct.RECURSIVE_CTE in constructs:
        risks.append(_risk("RECURSIVE_CTE"))
    external = [f for f in procedure.called_functions if not is_builtin_function(f)]
    if external:
        risks.append(_risk("EXTERNAL_ROUTINE_DEPENDENCY", routines=", ".join(sorted(external))))
    return tuple(risks)


def _risk(code: str, line: int | None = None, **values: object) -> SemanticRisk:
    spec = RISKS[code]
    return SemanticRisk(
        code=code, severity=spec.severity, message=spec.message.format(**values), line=line
    )


def _n_plus_one(loop: Statement) -> list[SemanticRisk]:
    """One finding per outermost loop, listing every statement that hits the database on
    each iteration (nested loops included)."""
    lines = [
        inner.line or 0
        for inner, depth in loop.walk()
        if depth > 0 and inner.kind in DATABASE_ROUND_TRIP_KINDS
    ]
    if not lines:
        return []
    return [
        _risk(
            "N_PLUS_ONE",
            loop.line,
            count=len(lines),
            plural="s" if len(lines) > 1 else "",
            loop=loop.kind.value.upper(),
            lines=", ".join(map(str, lines)),
        )
    ]


def _walk_all(statements: tuple[Statement, ...]) -> Iterator[tuple[Statement, int]]:
    for statement in statements:
        yield from statement.walk()


def recommend(
    constructs: Container[SqlConstruct], risks: tuple[SemanticRisk, ...]
) -> tuple[Recommendation, ...]:
    risk_codes = {risk.code for risk in risks}
    rules: list[tuple[bool, str]] = [
        (
            any(
                f in constructs
                for f in (
                    SqlConstruct.DML,
                    SqlConstruct.AGGREGATION,
                    SqlConstruct.JOIN,
                    SqlConstruct.CTE,
                )
            ),
            "KEEP_SET_BASED_SQL",
        ),
        ("N_PLUS_ONE" in risk_codes, "REWRITE_ROW_BY_ROW"),
        (
            SqlConstruct.TRANSACTION_CONTROL in constructs
            or SqlConstruct.ROW_LOCKING in constructs,
            "CALLER_OWNS_TRANSACTION",
        ),
        (
            SqlConstruct.RAISE in constructs or SqlConstruct.EXCEPTION_HANDLING in constructs,
            "TYPED_EXCEPTIONS",
        ),
        (SqlConstruct.OUT_PARAMETERS in constructs, "RESULT_TYPE_FOR_OUT_PARAMS"),
        (SqlConstruct.RETURN_QUERY in constructs, "TYPED_ROWS"),
        (SqlConstruct.GET_DIAGNOSTICS in constructs, "ROWCOUNT"),
        (SqlConstruct.DYNAMIC_SQL in constructs, "SAFE_DYNAMIC_SQL"),
    ]
    return tuple(
        Recommendation(code=code, message=RECOMMENDATIONS[code])
        for applies, code in rules
        if applies
    )
