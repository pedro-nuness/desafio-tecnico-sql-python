"""Evaluation of a modernization: does the generated module behave like the original routine?"""

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid7

from app.features.modernization.domain.enums import ModernizationStatus
from app.features.modernization.domain.modernization import Modernization
from app.shared.domain.value_object import ValueObject

BEHAVIORAL_EQUIVALENCE = "behavioral_equivalence"


class CaseResult(ValueObject):
    """One input of the evaluation dataset, run on the original and on the generated code."""

    name: str
    passed: bool
    detail: str
    """Why it is (not) equivalent, e.g. which table differs."""
    original: str
    """Observed outcome of the original routine (rows or error), for the report."""
    generated: str
    holdout: bool = False
    """Never shown to the LLM (the repair loop only sees the other cases)."""


class Evaluation(ValueObject):
    id: UUID
    modernization_id: UUID
    procedure_name: str
    metric: str = BEHAVIORAL_EQUIVALENCE
    prompt_version: str | None = None
    model: str | None = None
    static_valid: bool
    """The generated module passed ast.parse (the pipeline's blocking validation)."""
    completed: bool
    """The run ended with code (success or partial), not with an error."""
    cases: tuple[CaseResult, ...]
    created_at: datetime

    @classmethod
    def of(cls, modernization: Modernization, cases: tuple[CaseResult, ...]) -> Evaluation:
        report = modernization.report
        return cls(
            id=uuid7(),
            modernization_id=modernization.id,
            procedure_name=_bare_name(report.parsing.procedure_name if report.parsing else None),
            prompt_version=report.generation.prompt_version if report.generation else None,
            model=report.generation.model if report.generation else None,
            static_valid=bool(report.validation and report.validation.is_valid),
            completed=modernization.status
            in {ModernizationStatus.SUCCESS, ModernizationStatus.PARTIAL},
            cases=cases,
            created_at=datetime.now(UTC),
        )

    @property
    def cases_passed(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def cases_total(self) -> int:
        return len(self.cases)

    @property
    def score(self) -> float:
        """Share of dataset cases where the generated code behaves like the original."""
        return self.cases_passed / self.cases_total if self.cases else 0.0

    @property
    def equivalent(self) -> bool:
        return bool(self.cases) and self.cases_passed == self.cases_total

    @property
    def holdout_cases(self) -> tuple[CaseResult, ...]:
        return tuple(case for case in self.cases if case.holdout)

    @property
    def holdout_passed(self) -> int:
        return sum(case.passed for case in self.holdout_cases)

    @property
    def holdout_equivalent(self) -> bool:
        """Equivalent on the cases the repair loop never saw: the generalization signal."""
        holdout = self.holdout_cases
        return bool(holdout) and self.holdout_passed == len(holdout)


class EvaluationSummary(ValueObject):
    """Latest evaluation of each routine (e.g. annexes B-F) and the aggregated metrics."""

    evaluations: tuple[Evaluation, ...]

    @property
    def equivalence_rate(self) -> float:
        """Routines whose every case is equivalent / routines evaluated (the headline)."""
        return _rate(e.equivalent for e in self.evaluations)

    @property
    def holdout_equivalence_rate(self) -> float:
        """Same as equivalence_rate, on holdout cases only (honest after the repair loop)."""
        return _rate(e.holdout_equivalent for e in self.evaluations if e.holdout_cases)

    @property
    def holdout_case_pass_rate(self) -> float:
        total = sum(len(e.holdout_cases) for e in self.evaluations)
        return sum(e.holdout_passed for e in self.evaluations) / total if total else 0.0

    @property
    def case_pass_rate(self) -> float:
        total = sum(e.cases_total for e in self.evaluations)
        return sum(e.cases_passed for e in self.evaluations) / total if total else 0.0

    @property
    def static_valid_rate(self) -> float:
        return _rate(e.static_valid for e in self.evaluations)

    @property
    def completion_rate(self) -> float:
        return _rate(e.completed for e in self.evaluations)


def _rate(flags: Iterable[bool]) -> float:
    values = list(flags)
    return sum(values) / len(values) if values else 0.0


def _bare_name(qualified: str | None) -> str:
    return (qualified or "").rpartition(".")[2].lower()
