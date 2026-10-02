from collections.abc import Callable
from pathlib import Path

from app.features.modernization.analysis.analyzer import SemanticAnalyzer
from app.features.modernization.analysis.domain import (
    DependencyKind,
    GenerationStrategy,
    RiskSeverity,
    SemanticAnalysis,
    SqlConstruct,
)
from app.features.modernization.parsing.domain import (
    ExceptionHandler,
    ParsedProcedure,
    RoutineKind,
    SqlCommand,
    SqlFragment,
    Statement,
    StatementKind,
)
from app.features.modernization.parsing.plpgsql import PglastParser

analyzer = SemanticAnalyzer()
parser = PglastParser()


def _analyze(load_procedure: Callable[[str], str], name: str) -> SemanticAnalysis:
    return analyzer.analyze(parser.parse(load_procedure(name)))


def test_hybrid_procedure_constructs_and_risks(load_procedure: Callable[[str], str]) -> None:
    analysis = _analyze(load_procedure, "process_orders")

    assert {
        SqlConstruct.IN_PARAMETERS,
        SqlConstruct.OUT_PARAMETERS,
        SqlConstruct.VARIABLES,
        SqlConstruct.LOOP,
        SqlConstruct.EXCEPTION_HANDLING,
        SqlConstruct.RAISE,
        SqlConstruct.GET_DIAGNOSTICS,
        SqlConstruct.ROW_LOCKING,
        SqlConstruct.JSONB,
        SqlConstruct.FUNCTION_CALLS,
        SqlConstruct.DML,
    } <= set(analysis.construct_names)
    n_plus_one = [r for r in analysis.risks if r.code == "N_PLUS_ONE"]
    assert len(n_plus_one) == 1 and n_plus_one[0].severity is RiskSeverity.HIGH
    assert n_plus_one[0].line == 17  # the FOR loop; the UPDATE inside is listed
    assert "(line 24)" in n_plus_one[0].message
    assert analysis.parameters.inputs == ("p_customer_id",)
    assert analysis.parameters.outputs == ("total_amount",)
    assert analysis.parameters.in_out == ("processed_count",)
    assert analysis.recommended_strategy is GenerationStrategy.HYBRID
    assert {"REWRITE_ROW_BY_ROW", "CALLER_OWNS_TRANSACTION"} <= {
        r.code for r in analysis.recommendations
    }
    assert ("audit_log", DependencyKind.FUNCTION) in {
        (d.name, d.kind) for d in analysis.dependencies
    }


def test_set_based_function_is_delegated_to_database(load_procedure: Callable[[str], str]) -> None:
    analysis = _analyze(load_procedure, "monthly_sales_report")

    assert {
        SqlConstruct.CTE,
        SqlConstruct.AGGREGATION,
        SqlConstruct.JOIN,
        SqlConstruct.JSONB,
        SqlConstruct.RETURN_QUERY,
    } <= set(analysis.construct_names)
    assert analysis.recommended_strategy is GenerationStrategy.DATABASE_DELEGATED
    assert not [r for r in analysis.risks if r.severity is RiskSeverity.HIGH]


def test_pure_computation_is_reimplemented_in_python(load_procedure: Callable[[str], str]) -> None:
    analysis = _analyze(load_procedure, "calculate_discount")

    assert analysis.recommended_strategy is GenerationStrategy.PYTHON_REIMPLEMENTATION
    assert SqlConstruct.CONDITIONAL in analysis.construct_names


def test_procedure_transaction_dynamic_sql_cursor_recursive_cte(
    load_procedure: Callable[[str], str],
) -> None:
    analysis = _analyze(load_procedure, "archive_orders")
    risk_codes = {r.code for r in analysis.risks}

    assert {
        SqlConstruct.TRANSACTION_CONTROL,
        SqlConstruct.DYNAMIC_SQL,
        SqlConstruct.CURSOR,
        SqlConstruct.RECURSIVE_CTE,
    } <= set(analysis.construct_names)
    assert {"TRANSACTION_CONTROL", "DYNAMIC_SQL", "N_PLUS_ONE", "RECURSIVE_CTE"} <= risk_codes


def test_analyzer_works_on_hand_built_ir_without_any_parser() -> None:
    """The analyzer depends only on the domain IR (parser is swappable)."""
    swallowed = Statement(
        kind=StatementKind.BLOCK,
        line=1,
        body=(Statement(kind=StatementKind.SQL, line=2, sql=SqlFragment(text="DELETE FROM t")),),
        exception_handlers=(
            ExceptionHandler(conditions=("others",), body=(Statement(kind=StatementKind.OTHER),)),
        ),
    )
    procedure = ParsedProcedure(
        name="p",
        kind=RoutineKind.PROCEDURE,
        language="plpgsql",
        body=(swallowed,),
        parser="hand-built",
    )

    analysis = analyzer.analyze(procedure)

    assert "SWALLOWED_EXCEPTION" in {r.code for r in analysis.risks}
    assert analysis.recommended_strategy is GenerationStrategy.HYBRID


def test_unparsed_sql_becomes_a_risk() -> None:
    procedure = ParsedProcedure(
        name="p",
        kind=RoutineKind.FUNCTION,
        language="plpgsql",
        body=(
            Statement(
                kind=StatementKind.SQL,
                line=3,
                sql=SqlFragment(text="???", command=SqlCommand.OTHER, parse_error="syntax"),
            ),
        ),
        parser="hand-built",
    )

    risks = analyzer.analyze(procedure).risks

    assert [(r.code, r.line) for r in risks] == [("UNPARSED_SQL", 3)]


# --------------------------------------------------------------------------- annexes B-F

ANNEXES = Path(__file__).parents[2] / "examples" / "procedures"


def _annex(name: str) -> SemanticAnalysis:
    return analyzer.analyze(parser.parse((ANNEXES / f"{name}.sql").read_text(encoding="utf-8")))


def test_builtin_used_as_type_cast_is_not_an_external_routine() -> None:
    """Annex E filters with DATE(data_transacao): a PostgreSQL builtin, not a user routine."""
    analysis = _annex("e_sp_processar_lote_taxas")

    assert "EXTERNAL_ROUTINE_DEPENDENCY" not in {r.code for r in analysis.risks}
    assert DependencyKind.FUNCTION not in {d.kind for d in analysis.dependencies}


def test_one_n_plus_one_risk_per_loop_listing_every_round_trip() -> None:
    """Annex E runs 4 statements per cursor row: one finding for the loop, not four."""
    analysis = _annex("e_sp_processar_lote_taxas")

    [risk] = [r for r in analysis.risks if r.code == "N_PLUS_ONE"]
    assert risk.line == 34  # the LOOP
    assert "4 SQL statements" in risk.message
    assert "38, 60, 62, 65" in risk.message


def test_raise_warning_in_a_handler_does_not_count_as_re_raise() -> None:
    """Annex F logs a WARNING and returns a fallback row: the error is swallowed."""
    analysis = _annex("f_sp_relatorio_mensal_cliente")

    assert "SWALLOWED_EXCEPTION" in {r.code for r in analysis.risks}


def test_bare_raise_in_a_handler_re_raises() -> None:
    """Annex D logs the failure and re-raises with RAISE;: nothing is swallowed."""
    analysis = _annex("d_sp_transferir_entre_contas")

    assert "SWALLOWED_EXCEPTION" not in {r.code for r in analysis.risks}
