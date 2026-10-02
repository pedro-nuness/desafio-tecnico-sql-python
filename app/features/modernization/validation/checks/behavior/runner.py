"""Runs the cases of a scenario on both sides: the only place generated code executes.

Never in the server process. The harness calls run_in_subprocess, which starts this module with
the request as JSON on stdin; main runs the cases (CaseRunner) and prints the result as the last
stdout line, prefixed with RESULT_MARKER (the generated code may print too). A crash, a hang or
leaked module state stay contained. In production it belongs in a sandbox (container without
network, unprivileged database role).

    python -m app.features.modernization.validation.checks.behavior.runner < request.json
"""

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.features.modernization.parsing.domain import Parameter
from app.features.modernization.validation.checks.behavior.comparison import (
    Observed,
    canonical_rows,
    compare,
)
from app.features.modernization.validation.checks.behavior.domain import (
    Case,
    CaseResult,
    Scenario,
)
from app.features.modernization.validation.checks.behavior.generated import (
    INPUT_MODES,
    EntryPoint,
    coerce_args,
    defined_in,
    describe_error,
    load_entry_point,
)
from app.features.modernization.validation.checks.behavior.sandbox import Sandbox, Snapshot
from app.shared.errors import AppError

RUNNER_MODULE = "app.features.modernization.validation.checks.behavior.runner"
RESULT_MARKER = "@@evaluation-result@@ "
"""Prefix of the result line (generated code may print to stdout too)."""
PROJECT_ROOT = Path(__file__).resolve().parents[6]
"""The repository root (six packages up), so `python -m app...` resolves from there."""


# ---------------------------------------------------------------------- parent side


async def run_in_subprocess(
    request: dict[str, Any], *, timeout_seconds: float
) -> tuple[CaseResult, ...]:
    # subprocess.run in a worker thread: works on every event loop
    # (see validation/checks/lint.py).
    # Needed: a runner that hangs past the bound is a tool failure, reported as AppError.
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", RUNNER_MODULE],
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=timeout_seconds,
            cwd=PROJECT_ROOT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(f"Evaluation runner timed out after {timeout_seconds:.0f}s") from exc
    lines = completed.stdout.decode(errors="replace").splitlines()
    result = next(
        (
            line.removeprefix(RESULT_MARKER)
            for line in reversed(lines)
            if line.startswith(RESULT_MARKER)
        ),
        None,
    )
    if completed.returncode != 0 or result is None:
        stderr = completed.stderr.decode(errors="replace").strip().splitlines()
        raise AppError(
            "Evaluation runner failed: " + (stderr[-1] if stderr else "no output"),
            returncode=completed.returncode,
        )
    return tuple(CaseResult.model_validate(case) for case in json.loads(result))


# ---------------------------------------------------------------------- child side


async def main() -> None:
    request = json.loads(sys.stdin.read())
    sandbox = Sandbox(request["database_url"])
    try:
        cases = await CaseRunner(sandbox, request["case_timeout_seconds"]).run(
            routine=request["routine"],
            source_code=request["source_code"],
            code=request["code"],
            parameters=tuple(Parameter.model_validate(p) for p in request["parameters"]),
            scenario=Scenario.model_validate(request["scenario"]),
        )
    finally:
        await sandbox.close()
    payload = [case.model_dump(mode="json") for case in cases]
    print(RESULT_MARKER + json.dumps(payload), flush=True)


class CaseRunner:
    """Each case in a fresh schema; each side in its own connection, rolled back."""

    def __init__(self, sandbox: Sandbox, case_timeout_seconds: float) -> None:
        self._sandbox = sandbox
        self._timeout = case_timeout_seconds

    async def run(
        self,
        *,
        routine: str,
        source_code: str,
        code: str | None,
        parameters: Sequence[Parameter],
        scenario: Scenario,
    ) -> tuple[CaseResult, ...]:
        inputs = tuple(p for p in parameters if p.mode in INPUT_MODES)
        module_name = f"generated_{routine}_{uuid4().hex}"
        entry, problem = load_entry_point(code, routine, module_name)
        try:
            return tuple(
                [
                    await self._run_case(
                        scenario,
                        case,
                        original_sql=source_code,
                        entry=entry,
                        problem=problem,
                        args=coerce_args(case, inputs),
                        module_name=module_name,
                    )
                    for case in scenario.cases
                ]
            )
        finally:
            sys.modules.pop(module_name, None)

    async def _run_case(
        self,
        scenario: Scenario,
        case: Case,
        *,
        original_sql: str,
        entry: EntryPoint | None,
        problem: str | None,
        args: tuple[Any, ...],
        module_name: str,
    ) -> CaseResult:
        started = time.perf_counter()
        async with self._sandbox.schema("\n".join((scenario.setup_sql, original_sql))) as schema:
            snapshot = Snapshot(
                tables=scenario.compare_tables or await self._sandbox.tables(schema),
                ignore_columns=scenario.ignore_columns,
            )
            original = await self._observe_original(schema, snapshot, case.sql)
            if entry is None:
                generated = Observed(error=problem)
            else:
                generated = await self._observe_generated(
                    schema, snapshot, entry, args, module_name
                )
        passed, detail = compare(original, generated)
        return CaseResult(
            name=case.name,
            passed=passed,
            detail=detail,
            original=original.describe(),
            generated=generated.describe(),
            source=case.source,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    # A failing call is an observation (the metric), not an error to propagate. The snapshot is
    # inside the try too: code that swallows a database error returns normally but leaves the
    # transaction aborted, and that is a behavioral difference.

    async def _observe_original(self, schema: str, snapshot: Snapshot, sql: str) -> Observed:
        async with self._sandbox.connect(schema) as conn:
            # Needed: see above.
            try:
                async with asyncio.timeout(self._timeout):
                    result = await conn.exec_driver_sql(sql)
                    rows = [tuple(row) for row in result.all()] if result.returns_rows else None
                    state = await snapshot.take(conn)
            except Exception as exc:
                return Observed(error=describe_error(exc))
        # No rows (a procedure without OUT parameters): only the final state is compared.
        return Observed(rows=None if rows is None else canonical_rows(rows), state=state)

    async def _observe_generated(
        self,
        schema: str,
        snapshot: Snapshot,
        entry: EntryPoint,
        args: tuple[Any, ...],
        module_name: str,
    ) -> Observed:
        async with self._sandbox.connect(schema) as conn:
            # Needed: see above.
            try:
                async with asyncio.timeout(self._timeout):
                    value = await entry(conn, *args)
                    state = await snapshot.take(conn)
            except Exception as exc:
                # Only an exception class the module defines stands for the routine's RAISE.
                return Observed(error=describe_error(exc), deliberate=defined_in(exc, module_name))
        return Observed(rows=canonical_rows(value), state=state)


if __name__ == "__main__":
    asyncio.run(main())
