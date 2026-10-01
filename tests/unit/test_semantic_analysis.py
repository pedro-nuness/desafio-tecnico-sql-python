from collections.abc import Callable

from app.features.modernization.domain.enums import GenerationStrategy
from app.features.modernization.domain.models.parsing import (
    ExceptionHandler,
    ParsedProcedure,
    RoutineKind,
    SqlCommand,
    SqlFragment,
    Statement,
    StatementKind,
)
from app.features.modernization.domain.models.semantic_analysis import (
    DependencyKind,
    Feature,
    RiskSeverity,
    SemanticAnalysis,
)
from app.features.modernization.domain.services.semantic_analyzer import SemanticAnalyzer
from app.features.modernization.infrastructure.parsing.pglast_parser import PglastParser

analyzer = SemanticAnalyzer()
parser = PglastParser()


def _analyze(load_procedure: Callable[[str], str], name: str) -> SemanticAnalysis:
    return analyzer.analyze(parser.parse(load_procedure(name)))


def test_hybrid_procedure_features_and_risks(load_procedure: Callable[[str], str]) -> None:
    analysis = _analyze(load_procedure, "process_orders")

    assert {
        Feature.IN_PARAMETERS,
        Feature.OUT_PARAMETERS,
        Feature.VARIABLES,
        Feature.LOOP,
        Feature.EXCEPTION_HANDLING,
        Feature.RAISE,
        Feature.GET_DIAGNOSTICS,
        Feature.ROW_LOCKING,
        Feature.JSONB,
        Feature.FUNCTION_CALLS,
        Feature.DML,
    } <= set(analysis.feature_names)
    n_plus_one = [r for r in analysis.risks if r.code == "N_PLUS_ONE"]
    assert len(n_plus_one) == 1 and n_plus_one[0].severity is RiskSeverity.HIGH
    assert n_plus_one[0].line == 24
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
        Feature.CTE,
        Feature.AGGREGATION,
        Feature.JOIN,
        Feature.JSONB,
        Feature.RETURN_QUERY,
    } <= set(analysis.feature_names)
    assert analysis.recommended_strategy is GenerationStrategy.DATABASE_DELEGATED
    assert not [r for r in analysis.risks if r.severity is RiskSeverity.HIGH]


def test_pure_computation_is_reimplemented_in_python(load_procedure: Callable[[str], str]) -> None:
    analysis = _analyze(load_procedure, "calculate_discount")

    assert analysis.recommended_strategy is GenerationStrategy.PYTHON_REIMPLEMENTATION
    assert Feature.CONDITIONAL in analysis.feature_names


def test_procedure_transaction_dynamic_sql_cursor_recursive_cte(
    load_procedure: Callable[[str], str],
) -> None:
    analysis = _analyze(load_procedure, "archive_orders")
    risk_codes = {r.code for r in analysis.risks}

    assert {
        Feature.TRANSACTION_CONTROL,
        Feature.DYNAMIC_SQL,
        Feature.CURSOR,
        Feature.RECURSIVE_CTE,
    } <= set(analysis.feature_names)
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
