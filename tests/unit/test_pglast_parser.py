from collections.abc import Callable

import pytest

from app.features.modernization.domain.parsing import (
    DeclarationKind,
    ParameterMode,
    RoutineKind,
    SqlCommand,
    StatementKind,
)
from app.features.modernization.parsing.plpgsql import (
    PglastParser,
    analyze_sql,
)
from app.shared.errors import DomainError

parser = PglastParser()


def test_header_is_parsed_from_create_function(load_procedure: Callable[[str], str]) -> None:
    procedure = parser.parse(load_procedure("process_orders"))

    assert procedure.qualified_name == "billing.process_customer_orders"
    assert procedure.kind is RoutineKind.FUNCTION
    assert [(p.name, p.mode) for p in procedure.parameters] == [
        ("p_customer_id", ParameterMode.IN),
        ("total_amount", ParameterMode.OUT),
        ("processed_count", ParameterMode.INOUT),
    ]
    assert procedure.return_type == "record"
    assert procedure.parser == "pglast"


def test_body_is_a_structured_tree_with_source_lines(load_procedure: Callable[[str], str]) -> None:
    source = load_procedure("process_orders")
    procedure = parser.parse(source)
    lines = source.splitlines()

    loop = next(s for s, _ in procedure.iter_statements() if s.kind is StatementKind.FOR_QUERY)
    assert loop.sql is not None
    assert loop.sql.locking_clauses == ("FOR UPDATE",)
    assert [s.kind for s in loop.body][:2] == [StatementKind.SQL, StatementKind.GET_DIAGNOSTICS]
    assert lines[loop.body[0].line - 1].strip().startswith("UPDATE order_items")

    [block] = procedure.body
    assert block.exception_handlers[0].conditions == ("others",)
    assert {d.name: d.kind for d in procedure.declarations} == {
        "r_order": DeclarationKind.RECORD,
        "v_sum": DeclarationKind.VARIABLE,
        "v_rows": DeclarationKind.VARIABLE,
    }
    assert procedure.referenced_tables == ("order_items", "orders")
    assert "audit_log" in procedure.called_functions


def test_procedure_with_cursor_dynamic_sql_and_commit(load_procedure: Callable[[str], str]) -> None:
    procedure = parser.parse(load_procedure("archive_orders"))
    kinds = {s.kind for s, _ in procedure.iter_statements()}

    assert procedure.kind is RoutineKind.PROCEDURE
    assert procedure.return_type is None
    assert {StatementKind.OPEN_CURSOR, StatementKind.FETCH, StatementKind.DYNAMIC_SQL} <= kinds
    assert StatementKind.COMMIT in kinds
    cursor = next(d for d in procedure.declarations if d.kind is DeclarationKind.CURSOR)
    assert cursor.cursor_sql is not None and cursor.cursor_sql.tables == ("orders",)


def test_returns_table_and_return_query(load_procedure: Callable[[str], str]) -> None:
    procedure = parser.parse(load_procedure("monthly_sales_report"))

    assert procedure.returns_set is True
    assert procedure.return_type is not None and procedure.return_type.startswith("TABLE(")
    assert [s.kind for s in procedure.body] == [StatementKind.RETURN_QUERY]


def test_pct_type_parameter_is_rewritten_before_compilation() -> None:
    procedure = parser.parse(
        "CREATE FUNCTION f(p_id orders.id%TYPE) RETURNS int LANGUAGE plpgsql AS $$"
        " BEGIN RETURN p_id; END $$;"
    )

    assert procedure.parameters[0].data_type == "orders.id%TYPE"
    assert any("%TYPE" in warning for warning in procedure.warnings)


def test_assignment_is_split_with_the_lexer() -> None:
    procedure = parser.parse(
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$"
        " DECLARE x int; BEGIN x := compute_bonus(1) + 2; RETURN x; END $$;"
    )
    assign = procedure.body[0]

    assert (assign.kind, assign.target, assign.expression) == (
        StatementKind.ASSIGN,
        "x",
        "compute_bonus(1) + 2",
    )
    assert "compute_bonus" in procedure.called_functions


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("SELECT 1;", "No CREATE FUNCTION"),
        ("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;", "plpgsql"),
    ],
)
def test_unsupported_sources_raise_parsing_error(source: str, message: str) -> None:
    with pytest.raises(DomainError, match=message):
        parser.parse(source)


def test_invalid_body_raises_parsing_error(load_procedure: Callable[[str], str]) -> None:
    with pytest.raises(DomainError, match="Invalid SQL"):
        parser.parse(load_procedure("invalid_syntax"))


def test_embedded_sql_analysis() -> None:
    fragment = analyze_sql(
        "WITH RECURSIVE t AS (SELECT id FROM categories) "
        "SELECT o.data->>'k' FROM t JOIN public.orders o ON o.cat = t.id FOR UPDATE"
    )

    assert fragment.command is SqlCommand.SELECT
    assert fragment.tables == ("categories", "public.orders")  # CTE name excluded
    assert fragment.cte_names == ("t",)
    assert fragment.has_recursive_cte and fragment.has_join and fragment.uses_jsonb
    assert fragment.locking_clauses == ("FOR UPDATE",)


def test_unparseable_embedded_sql_is_reported_not_raised() -> None:
    fragment = analyze_sql("SELEC broken")
    assert fragment.parse_error is not None


def test_invalid_sql_propagates_native_error() -> None:
    with pytest.raises(DomainError, match="Invalid SQL"):
        parser.parse("not sql at all")
