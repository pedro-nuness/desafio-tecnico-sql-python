"""Behavioral equivalence: the generated module against the original routine (AD-16).

For each case of a scenario (sent by the caller; for the metric, read from
examples/evaluation/scenarios.yml) a fresh schema is created with the scenario's setup (tables,
seed) and the original routine. The case runs twice, each side in its own transaction that is
rolled back: the original through SQL, the generated entry point through Python. Then the two
sides are compared (what "equivalent" means: comparison.py).

Where each part lives, next to this module:

- runner.py: the subprocess that runs the cases, the only place generated code executes;
- sandbox.py: the evaluation database (throwaway schemas, final rows of the tables);
- generated.py: loading the generated module, its arguments, its errors;
- comparison.py: what each side did, and whether the two are equivalent.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from app.features.modernization.domain import Modernization
from app.features.modernization.parsing.domain import Parameter
from app.features.modernization.validation.checks.behavior.dataset import Dataset
from app.features.modernization.validation.checks.behavior.domain import (
    Case,
    CaseResult,
    Scenario,
)
from app.features.modernization.validation.checks.behavior.generated import describe_error
from app.features.modernization.validation.checks.behavior.runner import run_in_subprocess
from app.features.modernization.validation.checks.behavior.sandbox import Sandbox, sqlstate
from app.shared.errors import AppError, DomainError


class EquivalenceMetric(Protocol):
    """Port: runs the evaluation dataset of a recorded execution (fake in the unit tests)."""

    async def evaluate(self, modernization: Modernization) -> tuple[CaseResult, ...]: ...


class BehavioralEquivalence:
    """Runs the cases of a scenario on the original and on the generated code.

    Used twice: as a validation check inside the pipeline (run: the caller's scenario, feeding
    the repair loop; check.py) and as the metric of the experiment (evaluate: the dataset's
    scenario of the routine, holdout included, after the run). probe filters proposed cases.
    """

    def __init__(
        self,
        database_url: str | None,
        dataset_file: Path | None = None,
        *,
        case_timeout_seconds: float = 10.0,
    ) -> None:
        self._database_url = database_url
        self._dataset_file = dataset_file
        self._case_timeout_seconds = case_timeout_seconds
        self._sandbox = Sandbox(database_url)
        self._dataset: Dataset | None = None

    @property
    def configured(self) -> bool:
        return self._database_url is not None

    async def evaluate(self, modernization: Modernization) -> tuple[CaseResult, ...]:
        """The metric: every dataset case of the routine, holdout included."""
        parsing = modernization.report.parsing
        if parsing is None or not parsing.procedure_name:
            raise DomainError(
                "Nothing to evaluate: the routine was never parsed",
                execution_id=str(modernization.id),
            )
        routine = parsing.procedure_name.rpartition(".")[2].lower()
        dataset = self._load_dataset()
        scenario = dataset.scenario(routine, include_holdout=True)
        if scenario is None:
            raise DomainError(
                f"No evaluation scenario for routine {routine}",
                routine=routine,
                available=list(dataset.routines()),
            )
        cases = await self.run(
            routine=routine,
            source_code=modernization.source_code,
            code=modernization.generated_code,
            parameters=parsing.parameters,
            scenario=scenario,
        )
        holdout = dataset.holdout(routine)
        return tuple(case.model_copy(update={"holdout": case.name in holdout}) for case in cases)

    async def run(
        self,
        *,
        routine: str,
        source_code: str,
        code: str | None,
        parameters: Sequence[Parameter],
        scenario: Scenario,
    ) -> tuple[CaseResult, ...]:
        """Runs every case of the scenario, in order, in the runner subprocess."""
        if self._database_url is None:
            raise AppError("Evaluation database not configured: set EVALUATION_DATABASE_URL")
        request = {
            "routine": routine,
            "source_code": source_code,
            "code": code,
            "parameters": [parameter.model_dump(mode="json") for parameter in parameters],
            "scenario": scenario.model_dump(mode="json"),
            "database_url": self._database_url,
            "case_timeout_seconds": self._case_timeout_seconds,
        }
        # Generous bound: every case may hit its own timeout, plus interpreter start-up.
        timeout = self._case_timeout_seconds * (len(scenario.cases) + 1) + 30
        return await run_in_subprocess(request, timeout_seconds=timeout)

    async def probe(
        self, *, source_code: str, setup_sql: str, cases: Sequence[Case]
    ) -> tuple[str | None, ...]:
        """Runs each case on the original only, to filter proposed cases (case_generation).

        Per case, why it cannot be used, None = usable: the setup or the call is not valid
        SQL for this schema (SQLSTATE class 42), or it times out. An error the routine
        raises on purpose (RAISE, a constraint) is behavior, so the case is usable. No
        generated code runs here, so it runs in process.
        """
        # Needed: a broken setup (a proposed seed) discards the cases, it does not fail the run.
        try:
            async with self._sandbox.schema("\n".join((setup_sql, source_code))) as schema:
                return tuple([await self._probe_case(schema, case) for case in cases])
        except Exception as exc:
            return tuple(f"the setup failed: {describe_error(exc)}" for _ in cases)

    async def _probe_case(self, schema: str, case: Case) -> str | None:
        async with self._sandbox.connect(schema) as conn:
            # Needed: the error is the observation here, like in the runner.
            try:
                async with asyncio.timeout(self._case_timeout_seconds):
                    await conn.exec_driver_sql(case.sql)
            except TimeoutError:
                return f"timed out after {self._case_timeout_seconds:.0f}s on the original"
            except Exception as exc:
                if sqlstate(exc).startswith("42"):
                    return f"not valid SQL for this schema: {describe_error(exc)}"
        return None

    async def close(self) -> None:
        await self._sandbox.close()

    def _load_dataset(self) -> Dataset:
        if self._dataset_file is None:
            raise AppError("Evaluation dataset not configured: set EVALUATION_DATASET_FILE")
        if self._dataset is None:
            self._dataset = Dataset.load(self._dataset_file)
        return self._dataset
