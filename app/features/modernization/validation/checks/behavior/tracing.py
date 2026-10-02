"""The behavior harness with Langfuse spans (wired only when Langfuse is configured).

Inside the graph, under the node that called it:

- `behavior.run` (validation): one child span per case, failed ones marked as errors, and
  the score `behavior_pass_rate` (passed / total cases);
- `behavior.probe` (case_generation): one child span per proposed case, kept or discarded.

The cases run in the runner subprocess, so their spans are emitted when the results come
back: the real time of each case is in `duration_ms`, not in the span's duration.
"""

from collections.abc import Sequence

from app.features.modernization.parsing.domain import Parameter
from app.features.modernization.validation.checks.behavior.domain import (
    Case,
    CaseResult,
    Scenario,
)
from app.features.modernization.validation.checks.behavior.harness import BehavioralEquivalence
from app.shared.integrations.tracing import span

PASS_RATE_SCORE = "behavior_pass_rate"


class TracedEquivalence(BehavioralEquivalence):
    async def run(
        self,
        *,
        routine: str,
        source_code: str,
        code: str | None,
        parameters: Sequence[Parameter],
        scenario: Scenario,
    ) -> tuple[CaseResult, ...]:
        inputs = {"routine": routine, "cases": [case.name for case in scenario.cases]}
        async with span("behavior.run", inputs) as traced:
            results = await super().run(
                routine=routine,
                source_code=source_code,
                code=code,
                parameters=parameters,
                scenario=scenario,
            )
            for case, result in zip(scenario.cases, results, strict=True):
                await traced.child(
                    f"case: {case.name}",
                    inputs={"sql": case.sql, "args": case.args, "source": case.source},
                    output={
                        "detail": result.detail,
                        "original": result.original,
                        "generated": result.generated,
                        "duration_ms": result.duration_ms,
                    },
                    error=None if result.passed else _divergence(result),
                )
            passed = sum(result.passed for result in results)
            traced.output = {"passed": passed, "total": len(results)}
            traced.score(
                PASS_RATE_SCORE, passed / len(results), comment=f"{passed}/{len(results)} cases"
            )
        return results

    async def probe(
        self, *, source_code: str, setup_sql: str, cases: Sequence[Case]
    ) -> tuple[str | None, ...]:
        async with span("behavior.probe", {"cases": [case.name for case in cases]}) as traced:
            reasons = await super().probe(source_code=source_code, setup_sql=setup_sql, cases=cases)
            for case, reason in zip(cases, reasons, strict=True):
                # A discarded proposal is the filter working, not a failure: no error level.
                await traced.child(
                    f"case: {case.name}",
                    inputs={"sql": case.sql, "args": case.args},
                    output={"kept": reason is None, "reason": reason},
                )
            kept = sum(reason is None for reason in reasons)
            traced.output = {"kept": kept, "discarded": len(reasons) - kept}
        return reasons


def _divergence(result: CaseResult) -> str:
    return f"{result.detail}. The original {result.original}; the generated code {result.generated}"
