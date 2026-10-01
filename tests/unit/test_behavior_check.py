"""Behavior check inside validation: dev cases only, divergences as findings, skips reported."""

from pathlib import Path
from typing import Any

import pytest

from app.features.modernization.domain.evaluation import CaseResult, Evaluation, EvaluationSummary
from app.features.modernization.domain.modernization import (
    Modernization,
    ModernizationReport,
    ParsingReport,
    ValidationReport,
)
from app.features.modernization.evaluation.scenarios import Dataset
from app.features.modernization.parsing.plpgsql import PglastParser
from app.features.modernization.validation.behavior_check import BehaviorCheck
from app.features.modernization.validation.python_ast_check import PythonASTCheck
from app.features.modernization.validation.validate_code import (
    Routine,
    Rule,
    Skipped,
    ValidateCode,
)

EXAMPLES = Path(__file__).parents[2] / "examples"


def _routine(annex: str = "b_fn_saldo_cliente") -> Routine:
    source = (EXAMPLES / "procedures" / f"{annex}.sql").read_text(encoding="utf-8")
    return Routine(source_code=source, procedure=PglastParser().parse(source))


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
    def __init__(self, cases: tuple[CaseResult, ...] | None, *, configured: bool = True) -> None:
        self.configured = configured
        self._cases = cases
        self.calls: list[dict[str, Any]] = []

    async def run(self, **request: Any) -> tuple[CaseResult, ...] | None:
        self.calls.append(request)
        return self._cases


def _check(harness: _Harness) -> BehaviorCheck:
    return BehaviorCheck(harness)  # type: ignore[arg-type]


async def test_divergences_on_dev_cases_become_findings_for_the_repair_loop() -> None:
    harness = _Harness((_case("ok", True), _case("inactive account", False)))

    findings = await _check(harness).check("code", _routine())

    assert harness.calls[0]["include_holdout"] is False  # holdout never reaches the LLM
    assert harness.calls[0]["routine"] == "fn_saldo_cliente"
    assert not isinstance(findings, Skipped)
    [finding] = findings
    assert finding.code == "BEHAVIOR"
    assert "case 'inactive account'" in finding.message and "1800" in finding.message


@pytest.mark.parametrize(
    ("harness", "routine", "reason"),
    [
        (_Harness(()), None, "original routine"),
        (_Harness((), configured=False), _routine(), "EVALUATION_DATABASE_URL"),
        (_Harness(None), _routine(), "no evaluation scenario for routine fn_saldo_cliente"),
    ],
)
async def test_unverifiable_code_is_skipped_with_the_reason(
    harness: _Harness, routine: Routine | None, reason: str
) -> None:
    findings = await _check(harness).check("code", routine)

    assert isinstance(findings, Skipped) and reason in findings.reason


async def test_a_skipped_check_passes_and_is_reported_as_not_run() -> None:
    suite = ValidateCode(
        [
            Rule(PythonASTCheck(), blocking=True),
            Rule(_check(_Harness(None)), blocking=False),
        ]
    )

    result = await suite.execute("value = 1\n", _routine())

    behavior = next(r for r in result.results if r.validator == "behavior")
    assert behavior.success and behavior.skipped is not None
    assert result.passed_all  # nothing to retry on
    warnings = ValidationReport.from_result(result).warnings
    assert any(w.startswith("[behavior] not run: no evaluation scenario") for w in warnings)


def test_holdout_rates_count_only_cases_the_loop_never_saw() -> None:
    def evaluation(procedure: str, *cases: CaseResult) -> Evaluation:
        modernization = Modernization.start("src").model_copy(
            update={
                "report": ModernizationReport(
                    parsing=ParsingReport(success=True, procedure_name=procedure)
                )
            }
        )
        return Evaluation.of(modernization, cases)

    fitted = evaluation("a", _case("dev", True), _case("hold", False, holdout=True))
    general = evaluation("b", _case("dev", True), _case("hold", True, holdout=True))
    summary = EvaluationSummary(evaluations=(fitted, general))

    assert fitted.equivalent is False and fitted.holdout_equivalent is False
    assert general.holdout_equivalent
    assert summary.holdout_equivalence_rate == pytest.approx(0.5)
    assert summary.holdout_case_pass_rate == pytest.approx(0.5)


def test_every_routine_has_dev_and_holdout_cases() -> None:
    dataset = Dataset.load(EXAMPLES / "evaluation" / "scenarios.yml")
    for routine in dataset.routines():
        scenario = dataset.scenario(routine)
        assert scenario is not None
        holdout = [case.holdout for case in scenario.cases]
        assert any(holdout) and not all(holdout), routine


async def test_code_that_does_not_compile_is_left_to_python_ast() -> None:
    harness = _Harness(())

    findings = await _check(harness).check("def broken(:" + chr(10), _routine())

    assert isinstance(findings, Skipped) and "does not compile" in findings.reason
    assert harness.calls == []  # no subprocess, no repeated syntax error per case
