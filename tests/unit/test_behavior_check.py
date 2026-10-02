"""Behavior check inside validation: the caller's scenario, divergences as findings, skips
reported. The dev/holdout split belongs to the experiment's dataset only."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.features.modernization.domain import Modernization, ModernizationReport, ParsingSummary
from app.features.modernization.evaluation.domain import Evaluation, EvaluationSummary
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.schemas import ModernizationRequest
from app.features.modernization.validation.checks.behavior.check import BehaviorCheck
from app.features.modernization.validation.checks.behavior.dataset import Dataset
from app.features.modernization.validation.checks.behavior.domain import (
    Case,
    CaseResult,
    Scenario,
)
from app.features.modernization.validation.checks.syntax import PythonASTCheck
from app.features.modernization.validation.validate_code import (
    Routine,
    Rule,
    Skipped,
    ValidateCode,
)

EXAMPLES = Path(__file__).parents[2] / "examples"

SCENARIO = Scenario(
    setup_sql="CREATE TABLE contas (saldo numeric);",
    cases=(Case(name="client 1", sql="SELECT fn_saldo_cliente(1)", args=(1,)),),
)


def _routine(behavior: Scenario | None = SCENARIO) -> Routine:
    source = (EXAMPLES / "procedures" / "b_fn_saldo_cliente.sql").read_text(encoding="utf-8")
    return Routine(source_code=source, procedure=PglastParser().parse(source), behavior=behavior)


def _case(name: str, passed: bool, *, holdout: bool = False) -> CaseResult:
    return CaseResult(
        name=name,
        passed=passed,
        detail="result differs: original 1500 vs generated 1800" if not passed else "same",
        original="returned 1 row(s)",
        generated="returned 1 row(s)",
        holdout=holdout,
    )


class _Harness:
    def __init__(self, cases: tuple[CaseResult, ...], *, configured: bool = True) -> None:
        self.configured = configured
        self._cases = cases
        self.calls: list[dict[str, Any]] = []

    async def run(self, **request: Any) -> tuple[CaseResult, ...]:
        self.calls.append(request)
        return self._cases


def _check(harness: _Harness) -> BehaviorCheck:
    return BehaviorCheck(harness)  # type: ignore[arg-type]


async def test_divergences_on_the_callers_cases_become_findings_for_the_repair_loop() -> None:
    harness = _Harness((_case("ok", True), _case("inactive account", False)))

    findings = await _check(harness).check("code", _routine())

    assert harness.calls[0]["scenario"] == SCENARIO  # exactly what the caller sent
    assert harness.calls[0]["routine"] == "fn_saldo_cliente"
    assert not isinstance(findings, Skipped)
    [finding] = findings
    assert finding.code == "BEHAVIOR"
    assert "case 'inactive account'" in finding.message and "1800" in finding.message


@pytest.mark.parametrize(
    ("harness", "routine", "reason"),
    [
        (_Harness(()), None, "original routine"),
        (_Harness(()), _routine(behavior=None), "no behavior scenario provided"),
        (_Harness((), configured=False), _routine(), "EVALUATION_DATABASE_URL"),
    ],
)
async def test_unverifiable_code_is_skipped_with_the_reason(
    harness: _Harness, routine: Routine | None, reason: str
) -> None:
    findings = await _check(harness).check("code", routine)

    assert isinstance(findings, Skipped) and reason in findings.reason
    assert harness.calls == []


async def test_a_skipped_check_passes_and_is_reported_as_not_run() -> None:
    suite = ValidateCode(
        [
            Rule(PythonASTCheck(), blocking=True),
            Rule(_check(_Harness(())), blocking=False),
        ]
    )

    result = await suite.execute("value = 1\n", _routine(behavior=None))

    behavior = next(r for r in result.results if r.validator == "behavior")
    assert behavior.success and behavior.skipped is not None
    assert result.passed_all  # nothing to retry on
    warnings = result.warnings()
    assert any(w.startswith("[behavior] not run: no behavior scenario") for w in warnings)


async def test_code_that_does_not_compile_is_left_to_python_ast() -> None:
    harness = _Harness(())

    findings = await _check(harness).check("def broken(:" + chr(10), _routine())

    assert isinstance(findings, Skipped) and "does not compile" in findings.reason
    assert harness.calls == []  # no subprocess, no repeated syntax error per case


# --------------------------------------------------------------------------- request


def _request(**fields: Any) -> ModernizationRequest:
    behavior = {"seed": "INSERT INTO t VALUES (1);", "cases": [{"name": "c", "sql": "SELECT f()"}]}
    return ModernizationRequest.model_validate(
        {"source_code": "src", "behavior": behavior} | fields
    )


def test_the_request_scenario_runs_on_the_schema_plus_the_seed() -> None:
    command = _request(schema="CREATE TABLE t (x int);").to_command()

    assert command.behavior is not None
    assert command.behavior.setup_sql == "CREATE TABLE t (x int);\nINSERT INTO t VALUES (1);"
    assert [case.name for case in command.behavior.cases] == ["c"]
    assert command.behavior.compare_tables == ()  # every table the setup creates


def test_behavior_without_the_schema_is_rejected() -> None:
    with pytest.raises(ValidationError, match="needs `schema`"):
        _request()


def test_without_behavior_the_command_has_no_scenario() -> None:
    assert ModernizationRequest(source_code="src").to_command().behavior is None


# --------------------------------------------------------------------------- experiment


def test_holdout_rates_count_only_cases_the_loop_never_saw() -> None:
    def evaluation(procedure: str, *cases: CaseResult) -> Evaluation:
        modernization = Modernization.start("src").model_copy(
            update={"report": ModernizationReport(parsing=ParsingSummary(procedure_name=procedure))}
        )
        return Evaluation.of(modernization, cases)

    fitted = evaluation("a", _case("dev", True), _case("hold", False, holdout=True))
    general = evaluation("b", _case("dev", True), _case("hold", True, holdout=True))
    summary = EvaluationSummary(evaluations=(fitted, general))

    assert fitted.equivalent is False and fitted.holdout_equivalent is False
    assert general.holdout_equivalent
    assert summary.holdout_equivalence_rate == pytest.approx(0.5)
    assert summary.holdout_case_pass_rate == pytest.approx(0.5)


def test_the_pipeline_gets_the_dev_cases_and_the_metric_all_of_them() -> None:
    dataset = Dataset.load(EXAMPLES / "evaluation" / "scenarios.yml")
    for routine in dataset.routines():
        dev = dataset.scenario(routine, include_holdout=False)
        full = dataset.scenario(routine, include_holdout=True)
        assert dev is not None and full is not None
        holdout = dataset.holdout(routine)
        assert holdout, routine  # every routine keeps cases the LLM never sees
        assert {c.name for c in dev.cases} == {c.name for c in full.cases} - holdout
