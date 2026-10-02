"""Behavior check: the generated code against the original routine, inside the pipeline.

Runs the scenario of the run: the caller's (POST /modernize `behavior`: setup SQL and cases)
plus the cases case_generation kept, labeled "(generated)" in the feedback. Each
divergence becomes a finding, so the repair loop regenerates with it as feedback (AD-13).
Without a scenario the check is skipped and reported as not verified: building that data
automatically for any routine is the next step (README, Evolução futura).
"""

from app.features.modernization.validation.checks.behavior.domain import CaseResult, CaseSource
from app.features.modernization.validation.checks.behavior.harness import BehavioralEquivalence
from app.features.modernization.validation.domain import ValidationMessage
from app.features.modernization.validation.validate_code import Findings, Routine, Skipped


class BehaviorCheck:
    name = "behavior"

    def __init__(self, equivalence: BehavioralEquivalence) -> None:
        self._equivalence = equivalence

    async def check(self, code: str, routine: Routine | None = None) -> Findings:
        if routine is None:
            return Skipped("the original routine is not available")
        if routine.behavior is None:
            return Skipped("no behavior scenario provided (request field `behavior`)")
        if not self._equivalence.configured:
            return Skipped("EVALUATION_DATABASE_URL is not set")
        if not _compiles(code):
            # python_ast already reports it once; repeating it per case is noise in the feedback.
            return Skipped("the code does not compile (see python_ast)")
        cases = await self._equivalence.run(
            routine=routine.procedure.name.lower(),
            source_code=routine.source_code,
            code=code,
            parameters=routine.procedure.parameters,
            scenario=routine.behavior,
        )
        return tuple(
            ValidationMessage(
                code="BEHAVIOR",
                message=(
                    f"case '{case.name}'{_origin(case)}: {case.detail}. "
                    f"The original {case.original}; the generated code {case.generated}"
                ),
            )
            for case in cases
            if not case.passed
        )


def _origin(case: CaseResult) -> str:
    return " (generated)" if case.source is CaseSource.GENERATED else ""


def _compiles(code: str) -> bool:
    # Needed: a syntax error here only means "nothing to run", not a failure of this check.
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError:
        return False
    return True
