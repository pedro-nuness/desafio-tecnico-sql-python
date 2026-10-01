"""Behavior check: the generated code against the original routine, inside the pipeline.

Runs the dev cases of the evaluation dataset (holdout cases are kept for the metric). Each
divergence becomes a finding, so the repair loop regenerates with it as feedback (AD-13).
Routines without a scenario are skipped and reported as not verified: building that data
automatically for any routine is the next step (README, Evolução futura).
"""

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
        if not self._equivalence.configured:
            return Skipped("EVALUATION_DATABASE_URL is not set")
        if not _compiles(code):
            # python_ast already reports it once; repeating it per case is noise in the feedback.
            return Skipped("the code does not compile (see python_ast)")
        name = routine.procedure.name.lower()
        cases = await self._equivalence.run(
            routine=name,
            source_code=routine.source_code,
            code=code,
            parameters=routine.procedure.parameters,
            include_holdout=False,
        )
        if cases is None:
            return Skipped(f"no evaluation scenario for routine {name}")
        return tuple(
            ValidationMessage(
                code="BEHAVIOR",
                message=(
                    f"case '{case.name}': {case.detail}. "
                    f"The original {case.original}; the generated code {case.generated}"
                ),
            )
            for case in cases
            if not case.passed
        )


def _compiles(code: str) -> bool:
    # Needed: a syntax error here only means "nothing to run", not a failure of this check.
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError:
        return False
    return True
