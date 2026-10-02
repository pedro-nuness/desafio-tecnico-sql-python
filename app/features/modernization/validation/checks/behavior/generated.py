"""The generated module: loading its entry point, its arguments, and describing its errors."""

import inspect
import linecache
import sys
import traceback
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal
from types import ModuleType
from typing import Any

from app.features.modernization.parsing.domain import Parameter, ParameterMode
from app.features.modernization.validation.checks.behavior.comparison import PREVIEW_CHARS
from app.features.modernization.validation.checks.behavior.domain import Case
from app.shared.errors import AppError

INPUT_MODES = frozenset({ParameterMode.IN, ParameterMode.INOUT, ParameterMode.VARIADIC})
GENERATED_FILENAME_PREFIX = "<generated "

type EntryPoint = Callable[..., Awaitable[Any]]


def load_entry_point(
    code: str | None, routine: str, module_name: str
) -> tuple[EntryPoint | None, str | None]:
    """The generated module's `async def <routine>(conn, ...)`, or why it is unusable."""
    if not code:
        return None, "no generated code"
    module = ModuleType(module_name)
    sys.modules[module_name] = module  # dataclasses resolve their module by name
    filename = f"{GENERATED_FILENAME_PREFIX}{routine}>"
    # Tracebacks then show the generated source line (describe_error points the LLM to it).
    linecache.cache[filename] = (len(code), None, code.splitlines(keepends=True), filename)
    # Needed: a module that cannot even be imported fails every case, it is not a crash here.
    try:
        exec(compile(code, filename, "exec"), module.__dict__)
    except Exception as exc:
        return None, f"generated module failed to import: {describe_error(exc)}"
    entry = getattr(module, routine, None)
    if not inspect.iscoroutinefunction(entry):
        return None, f"entry point `async def {routine}(conn, ...)` not found"
    return entry, None


def coerce_args(case: Case, inputs: tuple[Parameter, ...]) -> tuple[Any, ...]:
    """The case's args (YAML/JSON values) as the Python types of the routine's input parameters."""
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
    """The exception is (a subclass of) a class the generated module defines."""
    return any(cls.__module__ == module_name for cls in type(exc).__mro__)


def describe_error(exc: BaseException) -> str:
    """Innermost cause, first line: `RaiseError: Saldo insuficiente ...`, not the wrapper.

    When the generated code raised it, the line it raised from is appended: a driver error
    such as `TypeError: expected str, got int` names no statement, and without it the repair
    attempt edited the wrong one.
    """
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    message = str(root).strip().splitlines()[0] if str(root).strip() else ""
    described = f"{type(exc if root is exc else root).__name__}: {message}"[:PREVIEW_CHARS]
    frames = [
        frame
        for frame in traceback.extract_tb(exc.__traceback__)
        if frame.filename.startswith(GENERATED_FILENAME_PREFIX)
    ]
    if not frames:
        return described
    frame = frames[-1]
    return f"{described} (at generated line {frame.lineno}: {frame.line})"
