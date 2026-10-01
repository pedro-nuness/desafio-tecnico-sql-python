"""Behavioral equivalence: the generated module against the original routine (AD-16).

For each case of the dataset (examples/evaluation/scenarios.yml) a fresh schema is created
with the Annex A tables, the seed and the original routine. The case runs twice, each side in
its own transaction that is rolled back: the original through SQL, the generated entry point
through Python. Equivalent means:

- same outcome: both return, or both raise. The Python exception must be a class the
  generated module defines (the prompt contract for RAISE EXCEPTION), so a crash such as a
  driver error or a TypeError never passes for the expected failure;
- same result rows, when the routine returns any (functions, OUT parameters, SETOF);
- same final rows in the compared tables, ignoring new ids and now() timestamps.

This executes LLM-generated code: only against a disposable database (EVALUATION_DATABASE_URL),
never the application's, and in a subprocess (evaluation/runner.py), never in the server
process: a crash, a hang or leaked module state stay contained. In production it belongs in a
sandbox (container without network, unprivileged database role).
"""

import asyncio
import dataclasses
import inspect
import json
import subprocess
import sys
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.features.modernization.domain.evaluation import CaseResult
from app.features.modernization.domain.modernization import Modernization
from app.features.modernization.domain.parsing import Parameter, ParameterMode
from app.features.modernization.evaluation.scenarios import Case, Dataset, Scenario
from app.shared.errors import AppError, DomainError

INPUT_MODES = frozenset({ParameterMode.IN, ParameterMode.INOUT, ParameterMode.VARIADIC})
PREVIEW_CHARS = 240
RUNNER_MODULE = "app.features.modernization.evaluation.runner"
RESULT_MARKER = "@@evaluation-result@@ "
"""Prefix of the runner's result line (generated code may print to stdout too)."""
PROJECT_ROOT = Path(__file__).resolve().parents[4]

type Run = Callable[[AsyncConnection], Awaitable[Any]]
type EntryPoint = Callable[..., Awaitable[Any]]


class EquivalenceMetric(Protocol):
    """Port: runs the evaluation dataset of a recorded execution (fake in the unit tests)."""

    async def evaluate(self, modernization: Modernization) -> tuple[CaseResult, ...]: ...


@dataclass(frozen=True, slots=True)
class Observed:
    """What one side did on one case."""

    rows: tuple[str, ...] | None = None
    """Canonical result rows; None when the routine returns nothing (procedure, no OUT)."""
    state: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Canonical rows of each compared table after the call (sorted)."""
    error: str | None = None
    deliberate: bool = False
    """The exception is a class the generated module defines (always False for SQL)."""

    def describe(self) -> str:
        if self.error is not None:
            return f"raised {self.error}"
        if self.rows is None:
            return "returned nothing"
        return f"returned {len(self.rows)} row(s): {_preview(self.rows)}"


class BehavioralEquivalence:
    """Runs the dataset cases of a routine on the original and on the generated code.

    Used twice: as the metric (every case, after the run) and as a validation check inside
    the pipeline (dev cases only, feeding the repair loop; validation/behavior_check.py).
    """

    def __init__(
        self,
        database_url: str | None,
        dataset_file: Path,
        *,
        case_timeout_seconds: float = 10.0,
        isolate: bool = True,
    ) -> None:
        self._database_url = database_url
        self._dataset_file = dataset_file
        self._case_timeout_seconds = case_timeout_seconds
        self._isolate = isolate
        """False only inside the runner subprocess itself."""
        self._engine: AsyncEngine | None = None
        self._dataset: Dataset | None = None

    @property
    def configured(self) -> bool:
        return self._database_url is not None

    async def evaluate(self, modernization: Modernization) -> tuple[CaseResult, ...]:
        """The metric: every case of the routine, holdout included."""
        parsing = modernization.report.parsing
        if parsing is None or not parsing.procedure_name:
            raise DomainError(
                "Nothing to evaluate: the routine was never parsed",
                execution_id=str(modernization.id),
            )
        routine = parsing.procedure_name.rpartition(".")[2].lower()
        cases = await self.run(
            routine=routine,
            source_code=modernization.source_code,
            code=modernization.generated_code,
            parameters=parsing.parameters,
            include_holdout=True,
        )
        if cases is None:
            raise DomainError(
                f"No evaluation scenario for routine {routine}",
                routine=routine,
                available=list(self._load_dataset().routines()),
            )
        return cases

    async def run(
        self,
        *,
        routine: str,
        source_code: str,
        code: str | None,
        parameters: Sequence[Parameter],
        include_holdout: bool,
    ) -> tuple[CaseResult, ...] | None:
        """Runs the routine's cases; None when the dataset has no scenario for it."""
        if self._database_url is None:
            raise AppError("Evaluation database not configured: set EVALUATION_DATABASE_URL")
        dataset = self._load_dataset()
        scenario = dataset.scenario(routine)
        if scenario is None:
            return None
        cases = tuple(case for case in scenario.cases if include_holdout or not case.holdout)
        if self._isolate:
            return await self._run_isolated(
                {
                    "routine": routine,
                    "source_code": source_code,
                    "code": code,
                    "parameters": [parameter.model_dump(mode="json") for parameter in parameters],
                    "include_holdout": include_holdout,
                    "database_url": self._database_url,
                    "dataset_file": str(self._dataset_file.resolve()),
                    "case_timeout_seconds": self._case_timeout_seconds,
                },
                cases=len(cases),
            )
        inputs = tuple(p for p in parameters if p.mode in INPUT_MODES)
        module_name = f"generated_{routine}_{uuid4().hex}"
        entry, problem = load_entry_point(code, routine, module_name)
        try:
            return tuple(
                [
                    await self._run_case(
                        dataset,
                        scenario,
                        case,
                        original_sql=source_code,
                        entry=entry,
                        problem=problem,
                        inputs=inputs,
                        module_name=module_name,
                    )
                    for case in cases
                ]
            )
        finally:
            sys.modules.pop(module_name, None)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    async def _run_isolated(
        self, request: dict[str, Any], *, cases: int
    ) -> tuple[CaseResult, ...] | None:
        # Generous bound: every case may hit its own timeout, plus interpreter start-up.
        timeout = self._case_timeout_seconds * (cases + 1) + 30
        # subprocess.run in a worker thread: works on every event loop (see ruff_check.py).
        # Needed: a runner that hangs past the bound is a tool failure, reported as AppError.
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", RUNNER_MODULE],
                input=json.dumps(request).encode(),
                capture_output=True,
                timeout=timeout,
                cwd=PROJECT_ROOT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError(f"Evaluation runner timed out after {timeout:.0f}s") from exc
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
        payload = json.loads(result)
        return None if payload is None else tuple(CaseResult.model_validate(c) for c in payload)

    # ------------------------------------------------------------------ one case

    async def _run_case(
        self,
        dataset: Dataset,
        scenario: Scenario,
        case: Case,
        *,
        original_sql: str,
        entry: EntryPoint | None,
        problem: str | None,
        inputs: tuple[Parameter, ...],
        module_name: str,
    ) -> CaseResult:
        setup = "\n".join((dataset.setup_sql, dataset.requirements_sql(scenario), original_sql))
        args = coerce_args(case, inputs)

        async def call_original(conn: AsyncConnection) -> Any:
            result = await conn.exec_driver_sql(case.sql)
            return [tuple(row) for row in result.all()] if result.returns_rows else None

        async def call_generated(conn: AsyncConnection) -> Any:
            assert entry is not None
            return await entry(conn, *args)

        async with self._sandbox(setup) as schema:
            original = await self._observe(schema, dataset, call_original, module_name=None)
            if entry is None:
                generated = Observed(error=problem)
            else:
                generated = await self._observe(
                    schema, dataset, call_generated, module_name=module_name
                )
        passed, detail = compare(original, generated)
        return CaseResult(
            name=case.name,
            passed=passed,
            detail=detail,
            original=original.describe(),
            generated=generated.describe(),
            holdout=case.holdout,
        )

    async def _observe(
        self, schema: str, dataset: Dataset, run: Run, *, module_name: str | None
    ) -> Observed:
        async with self._engine_or_fail().connect() as conn:
            await conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            # Needed: a failing call is an observation (the metric), not an error to propagate.
            # The snapshot is inside too: code that swallows a database error returns normally
            # but leaves the transaction aborted, and that is a behavioral difference.
            try:
                async with asyncio.timeout(self._case_timeout_seconds):
                    value = await run(conn)
                state = await _snapshot(conn, dataset)
            except Exception as exc:
                deliberate = module_name is not None and defined_in(exc, module_name)
                return Observed(error=describe_error(exc), deliberate=deliberate)
            await conn.rollback()
        rows = None if value is None and module_name is None else canonical_rows(value)
        return Observed(rows=rows, state=state)

    @asynccontextmanager
    async def _sandbox(self, setup_sql: str) -> AsyncIterator[str]:
        """A throwaway schema with the legacy tables, the seed and the routines."""
        schema = f"eval_{uuid4().hex}"
        await self._run_script(
            f'CREATE SCHEMA "{schema}"; SET search_path TO "{schema}";\n{setup_sql}'
        )
        try:
            yield schema
        finally:
            await self._run_script(f'DROP SCHEMA "{schema}" CASCADE')

    async def _run_script(self, script: str) -> None:
        # Multi-statement scripts (DDL, $$ bodies) need the driver's simple query protocol.
        async with self._engine_or_fail().connect() as conn:
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(script)  # type: ignore[union-attr]

    def _engine_or_fail(self) -> AsyncEngine:
        if self._database_url is None:
            raise AppError("Evaluation database not configured: set EVALUATION_DATABASE_URL")
        if self._engine is None:
            # NullPool: every connection is fresh, so a session setting never leaks.
            self._engine = create_async_engine(self._database_url, poolclass=NullPool)
        return self._engine

    def _load_dataset(self) -> Dataset:
        if self._dataset is None:
            self._dataset = Dataset.load(self._dataset_file)
        return self._dataset


async def _snapshot(conn: AsyncConnection, dataset: Dataset) -> dict[str, tuple[str, ...]]:
    state: dict[str, tuple[str, ...]] = {}
    for table in dataset.compare_tables:
        quoted = conn.dialect.identifier_preparer.quote(table)  # name from the dataset file
        result = await conn.execute(
            # ::text: parsed here with Decimal, so NUMERIC values never go through float.
            text(f"SELECT (to_jsonb(t) - CAST(:ignored AS text[]))::text FROM {quoted} AS t"),  # noqa: S608
            {"ignored": list(dataset.ignore_columns)},
        )
        state[table] = tuple(
            sorted(
                canonical(json.loads(raw, parse_float=Decimal, parse_int=Decimal))
                for (raw,) in result.all()
            )
        )
    return state


# ---------------------------------------------------------------------- generated module


def load_entry_point(
    code: str | None, routine: str, module_name: str
) -> tuple[EntryPoint | None, str | None]:
    """The generated module's `async def <routine>(conn, ...)`, or why it is unusable."""
    if not code:
        return None, "no generated code"
    module = ModuleType(module_name)
    sys.modules[module_name] = module  # dataclasses resolve their module by name
    # Needed: a module that cannot even be imported fails every case, it is not a crash here.
    try:
        exec(compile(code, f"<generated {routine}>", "exec"), module.__dict__)
    except Exception as exc:
        return None, f"generated module failed to import: {describe_error(exc)}"
    entry = getattr(module, routine, None)
    if not inspect.iscoroutinefunction(entry):
        return None, f"entry point `async def {routine}(conn, ...)` not found"
    return entry, None


def coerce_args(case: Case, inputs: tuple[Parameter, ...]) -> tuple[Any, ...]:
    if len(case.args) != len(inputs):
        raise AppError(
            f"Case {case.name!r} has {len(case.args)} args; the routine takes {len(inputs)}",
            case=case.name,
        )
    return tuple(
        _coerce(value, parameter.data_type)
        for value, parameter in zip(case.args, inputs, strict=True)
    )


def _coerce(value: Any, data_type: str) -> Any:
    kind = data_type.lower()
    if value is None:
        return None
    if kind.startswith(("numeric", "decimal", "money")):
        return Decimal(str(value))
    if kind.startswith(("int", "bigint", "smallint")):
        return int(value)
    if kind == "date":
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    if kind.startswith("timestamp"):
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return value


def defined_in(exc: BaseException, module_name: str) -> bool:
    return any(cls.__module__ == module_name for cls in type(exc).__mro__)


def describe_error(exc: BaseException) -> str:
    """Innermost cause, first line: `RaiseError: Saldo insuficiente ...`, not the wrapper."""
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    message = str(root).strip().splitlines()[0] if str(root).strip() else ""
    return f"{type(exc if root is exc else root).__name__}: {message}"[:PREVIEW_CHARS]


# ---------------------------------------------------------------------- comparison


def compare(original: Observed, generated: Observed) -> tuple[bool, str]:
    if original.error is not None and generated.error is not None:
        if generated.deliberate:
            return True, "both raised"
        return False, f"generated code crashed instead of raising its own error: {generated.error}"
    if original.error is not None:
        return False, "the original raised; the generated code returned normally"
    if generated.error is not None:
        return False, f"the generated code raised: {generated.error}"
    if original.rows is not None and original.rows != generated.rows:
        return False, (
            f"result differs: original {_preview(original.rows)} "
            f"vs generated {_preview(generated.rows or ())}"
        )
    diffs = [
        _table_diff(table, rows, generated.state.get(table, ()))
        for table, rows in original.state.items()
        if rows != generated.state.get(table, ())
    ]
    if diffs:
        return False, "; ".join(diffs)
    return True, "same result and same final state"


def _table_diff(table: str, original: tuple[str, ...], generated: tuple[str, ...]) -> str:
    only_original = list((Counter(original) - Counter(generated)).elements())
    only_generated = list((Counter(generated) - Counter(original)).elements())
    parts = [f"{table}: {len(only_original)} row(s) only in the original"]
    if only_original:
        parts.append(f"e.g. {_preview(only_original[:1])}")
    parts.append(f"{len(only_generated)} only in the generated")
    if only_generated:
        parts.append(f"e.g. {_preview(only_generated[:1])}")
    return " ".join(parts)


def canonical_rows(value: Any) -> tuple[str, ...]:
    """Python return value -> canonical rows: None, a scalar, a record (dataclass, tuple,
    mapping, Row) or a list of records."""
    if value is None:
        return ()
    records = value if _is_row_list(value) else [value]
    return tuple(canonical(list(_record(item))) for item in records)


def _is_row_list(value: Any) -> bool:
    if isinstance(value, list):
        return True
    return isinstance(value, tuple) and bool(value) and all(map(_is_record, value))


def _is_record(value: Any) -> bool:
    return (dataclasses.is_dataclass(value) and not isinstance(value, type)) or isinstance(
        value, Mapping | tuple
    )


def _record(value: Any) -> tuple[Any, ...]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return tuple(getattr(value, f.name) for f in dataclasses.fields(value))
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(value)
    return (value,)


def canonical(value: Any) -> str:
    """Stable text for comparison: numbers by value (1500 == 1500.00 == 1500.0), dates ISO."""
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False)


def _plain(value: Any) -> Any:
    match value:
        case bool() | None:
            return value
        case int() | float() | Decimal():
            number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
            return "0" if number == 0 else format(number.normalize(), "f")
        case datetime() | date():
            return value.isoformat()
        case Mapping():
            return {str(key): _plain(item) for key, item in value.items()}
        case list() | tuple():
            return [_plain(item) for item in value]
        case _:
            return str(value)


def _preview(rows: Sequence[str]) -> str:
    text_ = ", ".join(rows[:3]) + (" ..." if len(rows) > 3 else "")
    return text_ if len(text_) <= PREVIEW_CHARS else text_[: PREVIEW_CHARS - 3] + "..."
