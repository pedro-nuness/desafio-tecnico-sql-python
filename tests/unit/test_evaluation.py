"""Evaluation metric without a database: comparison rules, dataset, use cases."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.features.modernization.domain import Modernization, ModernizationReport, ParsingSummary
from app.features.modernization.evaluation.domain import Evaluation, EvaluationSummary
from app.features.modernization.parsing.domain import Parameter, ParameterMode
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.use_cases import (
    EvaluateCommand,
    EvaluateModernization,
    EvaluationSummaryQuery,
    GetEvaluationSummary,
    ModernizeCommand,
)
from app.features.modernization.validation.checks.behavior.comparison import (
    Observed,
    canonical,
    canonical_rows,
    compare,
)
from app.features.modernization.validation.checks.behavior.dataset import Dataset
from app.features.modernization.validation.checks.behavior.domain import Case, CaseResult
from app.features.modernization.validation.checks.behavior.generated import (
    INPUT_MODES,
    coerce_args,
    defined_in,
    describe_error,
    load_entry_point,
)
from app.shared.errors import AppError, DomainError, NotFoundError
from tests.conftest import ModernizeFactory
from tests.fakes import (
    FakeMetric,
    InMemoryDatabase,
    InMemoryEvaluationRepository,
    InMemoryModernizationRepository,
)

EXAMPLES = Path(__file__).parents[2] / "examples"
DATASET = EXAMPLES / "evaluation" / "scenarios.yml"

# --------------------------------------------------------------------------- canonical form


def test_numbers_compare_by_value_whatever_their_python_type() -> None:
    assert canonical([Decimal("1500.00")]) == canonical([1500]) == canonical([1500.0])
    assert canonical([Decimal("5.01")]) != canonical([Decimal("5.00")])
    assert canonical([Decimal("0.00")]) == canonical([Decimal("-0")])


def test_dates_and_nested_json_are_canonical() -> None:
    assert canonical({"b": date(2026, 9, 1), "a": [1]}) == '{"a": ["1"], "b": "2026-09-01"}'


@dataclass(frozen=True)
class _Row:
    month: date
    total: Decimal


@pytest.mark.parametrize(
    ("value", "rows"),
    [
        (None, ()),
        (Decimal("1500.00"), ('["1500"]',)),
        (_Row(date(2026, 7, 1), Decimal("2.50")), ('["2026-07-01", "2.5"]',)),
        ([_Row(date(2026, 7, 1), Decimal("1")), _Row(date(2026, 8, 1), Decimal("2"))], 2),
        ((_Row(date(2026, 7, 1), Decimal("1")),), 1),  # a tuple of records is a list of rows
        ((2, "ok"), ('["2", "ok"]',)),  # a tuple of scalars is one row
        ({"afetadas": 2}, ('["2"]',)),
    ],
)
def test_python_results_become_rows(value: object, rows: tuple[str, ...] | int) -> None:
    result = canonical_rows(value)
    assert len(result) == rows if isinstance(rows, int) else result == rows


# --------------------------------------------------------------------------- outcome rules


def _ok(rows: tuple[str, ...] | None = ("x",), **state: tuple[str, ...]) -> Observed:
    return Observed(rows=rows, state=state)


def test_both_raising_is_equivalent_only_if_the_python_error_is_deliberate() -> None:
    original = Observed(error="RaiseError: Saldo insuficiente")

    assert compare(original, Observed(error="SaldoInsuficienteError: x", deliberate=True))[0]
    passed, detail = compare(original, Observed(error="TypeError: bug", deliberate=False))
    assert not passed and "crashed" in detail


def test_one_side_raising_is_a_difference() -> None:
    assert not compare(Observed(error="RaiseError: x"), _ok())[0]
    assert not compare(_ok(), Observed(error="X: y", deliberate=True))[0]


def test_rows_are_compared_only_when_the_original_returns_some() -> None:
    assert compare(_ok(rows=None), _ok(rows=('["summary"]',)))[0]  # procedure without OUT
    passed, detail = compare(_ok(rows=('["5.01"]',)), _ok(rows=('["5"]',)))
    assert not passed and "result differs" in detail


def test_final_state_differences_name_the_table_and_the_rows() -> None:
    passed, detail = compare(
        _ok(contas=('{"saldo": "993.99"}',)), _ok(contas=('{"saldo": "999"}',))
    )

    assert not passed
    assert "contas: 1 row(s) only in the original" in detail and "993.99" in detail


# --------------------------------------------------------------------------- generated module

GOOD = """
from decimal import Decimal

class SaldoError(Exception):
    pass

async def fn_saldo(conn, cliente_id: int) -> Decimal:
    if cliente_id < 0:
        raise SaldoError("negative")
    return Decimal("1")
"""


def test_entry_point_is_the_async_function_named_like_the_routine() -> None:
    entry, problem = load_entry_point(GOOD, "fn_saldo", f"generated_{uuid4().hex}")

    assert entry is not None and problem is None


@pytest.mark.parametrize(
    ("code", "problem"),
    [
        (None, "no generated code"),
        ("from sqlalchemy.engine import AsyncConnection\n", "failed to import: ImportError"),
        ("def fn_saldo(conn): ...\n", "entry point `async def fn_saldo"),
    ],
)
def test_unusable_modules_say_why(code: str | None, problem: str) -> None:
    entry, reason = load_entry_point(code, "fn_saldo", f"generated_{uuid4().hex}")

    assert entry is None and reason is not None and problem in reason


async def test_exceptions_defined_by_the_module_are_deliberate() -> None:
    name = f"generated_{uuid4().hex}"
    entry, _ = load_entry_point(GOOD, "fn_saldo", name)
    assert entry is not None
    with pytest.raises(Exception) as exc_info:
        await entry(None, -1)

    assert defined_in(exc_info.value, name)
    assert not defined_in(ValueError("x"), name)


def test_errors_are_described_by_their_innermost_cause() -> None:
    try:
        try:
            raise LookupError("Saldo insuficiente: saldo=200.00\nCONTEXT: PL/pgSQL")
        except LookupError as inner:
            raise RuntimeError("(sqlalchemy wrapper) long text") from inner
    except RuntimeError as exc:
        assert describe_error(exc) == "LookupError: Saldo insuficiente: saldo=200.00"


async def test_errors_raised_by_generated_code_point_to_its_line() -> None:
    # A driver TypeError names no statement: the repair attempt needs the line it came from.
    code = "async def fn(conn, days):\n    ok = 1\n    return int('x' + days)\n"
    entry, problem = load_entry_point(code, "fn", f"generated_{uuid4().hex}")
    assert entry is not None and problem is None
    with pytest.raises(TypeError) as exc_info:
        await entry(None, 30)
    assert describe_error(exc_info.value) == (
        'TypeError: can only concatenate str (not "int") to str'
        " (at generated line 3: return int('x' + days))"
    )


def test_arguments_take_the_types_of_the_routine_parameters() -> None:
    inputs = tuple(
        Parameter(name=n, data_type=t, mode=ParameterMode.IN)
        for n, t in [("a", "bigint"), ("b", "numeric(18,2)"), ("c", "date"), ("d", "integer")]
    )
    case = Case(name="c", sql="CALL x()", args=(10, "50.00", "2026-09-15", None))

    assert coerce_args(case, inputs) == (10, Decimal("50.00"), date(2026, 9, 15), None)
    with pytest.raises(AppError, match="has 1 args"):
        coerce_args(Case(name="bad", sql="x", args=(1,)), inputs)


# --------------------------------------------------------------------------- dataset


def test_every_annex_has_a_scenario_whose_cases_match_its_signature() -> None:
    dataset = Dataset.load(DATASET)
    for procedure_file in sorted((EXAMPLES / "procedures").glob("*.sql")):
        procedure = PglastParser().parse(procedure_file.read_text(encoding="utf-8"))
        scenario = dataset.scenario(procedure.name, include_holdout=True)
        assert scenario is not None, procedure.name
        inputs = [p for p in procedure.parameters if p.mode in INPUT_MODES]
        for case in scenario.cases:
            assert len(case.args) == len(inputs), (procedure.name, case.name)
            assert procedure.name in case.sql
    assert "CREATE TABLE contas" in dataset.setup_sql and "setval" in dataset.setup_sql


# --------------------------------------------------------------------------- domain + use cases


def _case(passed: bool) -> CaseResult:
    return CaseResult(name="c", passed=passed, detail="", original="", generated="")


def _evaluation(procedure: str, *passed: bool, static_valid: bool = True) -> Evaluation:
    modernization = Modernization.start("src").model_copy(
        update={
            "report": ModernizationReport(
                parsing=ParsingSummary(procedure_name=f"public.{procedure}")
            )
        }
    )
    evaluation = Evaluation.of(modernization, tuple(_case(p) for p in passed))
    return evaluation.model_copy(update={"static_valid": static_valid})


def test_score_and_equivalence() -> None:
    evaluation = _evaluation("FN_X", True, False, True)

    assert evaluation.procedure_name == "fn_x"
    assert (evaluation.cases_passed, evaluation.cases_total) == (2, 3)
    assert evaluation.score == pytest.approx(2 / 3)
    assert not evaluation.equivalent
    assert not _evaluation("fn_y").equivalent  # no cases: nothing was shown to be equivalent


def test_summary_rates() -> None:
    summary = EvaluationSummary(
        evaluations=(
            _evaluation("a", True, True),
            _evaluation("b", True, False),
            _evaluation("c", False, static_valid=False),
        )
    )

    assert summary.equivalence_rate == pytest.approx(1 / 3)
    assert summary.case_pass_rate == pytest.approx(3 / 5)
    assert summary.static_valid_rate == pytest.approx(2 / 3)


async def test_evaluate_stores_the_result_of_the_recorded_execution(
    make_modernize: ModernizeFactory,
    store: InMemoryDatabase,
    load_procedure: Callable[[str], str],
) -> None:
    created = await make_modernize().execute(ModernizeCommand(load_procedure("process_orders")))
    metric = FakeMetric([_case(True), _case(False)])
    use_case = EvaluateModernization(
        InMemoryModernizationRepository(store), InMemoryEvaluationRepository(store), metric
    )

    evaluation = await use_case.execute(EvaluateCommand(created.id))

    assert metric.evaluated == [created]
    assert store.evaluations == [evaluation]
    assert evaluation.modernization_id == created.id
    assert evaluation.procedure_name == "process_customer_orders"
    assert evaluation.prompt_version is not None and evaluation.static_valid
    summary = await GetEvaluationSummary(InMemoryEvaluationRepository(store)).execute(
        EvaluationSummaryQuery()
    )
    assert summary.evaluations == (evaluation,)


async def test_evaluate_unknown_execution_or_unknown_routine_stores_nothing(
    make_modernize: ModernizeFactory,
    store: InMemoryDatabase,
    load_procedure: Callable[[str], str],
) -> None:
    created = await make_modernize().execute(ModernizeCommand(load_procedure("process_orders")))

    def use_case(metric: FakeMetric) -> EvaluateModernization:
        return EvaluateModernization(
            InMemoryModernizationRepository(store),
            InMemoryEvaluationRepository(store),
            metric,
        )

    with pytest.raises(NotFoundError):
        await use_case(FakeMetric()).execute(EvaluateCommand(uuid4()))
    with pytest.raises(DomainError):
        await use_case(FakeMetric(error=DomainError("No evaluation scenario"))).execute(
            EvaluateCommand(created.id)
        )
    assert store.evaluations == []
