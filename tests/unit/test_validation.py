"""Validation: the checks (strategies) report findings; ValidateCode applies the rules."""

import asyncio

import pytest

from app.features.modernization.validation.checks.lint import RuffCheck
from app.features.modernization.validation.checks.syntax import PythonASTCheck
from app.features.modernization.validation.domain import ValidationMessage
from app.features.modernization.validation.validate_code import Rule, ValidateCode
from app.shared.errors import AppError
from tests.conftest import VALID_CODE, default_validate_code


async def test_ast_check_accepts_valid_python() -> None:
    assert await PythonASTCheck().check(VALID_CODE) == ()


async def test_ast_check_reports_syntax_errors_with_position() -> None:
    [message] = await PythonASTCheck().check("def broken(:\n    pass\n")

    assert message.line == 1


async def test_ast_check_accepts_python_314_syntax() -> None:
    code = "def first[T](items: list[T]) -> T:\n    return items[0]\n"
    assert await PythonASTCheck().check(code) == ()


async def test_ruff_reports_findings() -> None:
    messages = await RuffCheck().check("import os\n\n\ndef f():\n    return undefined_name\n")

    assert {"F401", "F821"} <= {m.code for m in messages}


async def test_ruff_flags_sql_built_with_string_formatting() -> None:
    code = 'def q(table: str) -> str:\n    return f"SELECT * FROM {table} WHERE id = 1"\n'

    assert "S608" in {m.code for m in await RuffCheck().check(code)}


async def test_ruff_accepts_clean_code() -> None:
    assert await RuffCheck().check(VALID_CODE) == ()


async def test_a_failing_blocking_rule_makes_the_code_invalid() -> None:
    result = await default_validate_code().execute("def broken(:\n    pass\n")

    ast_result = next(r for r in result.results if r.validator == "python_ast")
    assert not ast_result.success and ast_result.blocking
    assert not result.is_valid


async def test_a_failing_non_blocking_rule_only_makes_it_partial() -> None:
    result = await default_validate_code().execute("import os\n\nvalue = 1\n")

    ruff_result = next(r for r in result.results if r.validator == "ruff")
    assert not ruff_result.success and not ruff_result.blocking
    assert result.is_valid and not result.passed_all


async def test_blocking_is_decided_by_the_rule_not_by_the_check() -> None:
    strict = ValidateCode([Rule(RuffCheck(), blocking=True)])

    result = await strict.execute("import os\n\nvalue = 1\n")

    assert not result.is_valid


async def test_checks_run_concurrently() -> None:
    both_running = asyncio.Barrier(2)

    class _WaitsForTheOther:
        def __init__(self, name: str) -> None:
            self.name = name

        async def check(self, code: str, routine: object = None) -> tuple[ValidationMessage, ...]:
            await asyncio.wait_for(both_running.wait(), timeout=1)
            return ()

    suite = ValidateCode([Rule(_WaitsForTheOther(n), blocking=True) for n in ("a", "b")])

    assert (await suite.execute(VALID_CODE)).passed_all


class _BrokenCheck:
    name = "broken"

    async def check(self, code: str, routine: object = None) -> tuple[ValidationMessage, ...]:
        raise AppError("binary missing")


async def test_check_failures_propagate() -> None:
    suite = ValidateCode(
        [Rule(PythonASTCheck(), blocking=True), Rule(_BrokenCheck(), blocking=False)]
    )
    with pytest.raises(AppError, match="binary missing"):
        await suite.execute(VALID_CODE)


def test_at_least_one_rule_is_required() -> None:
    with pytest.raises(ValueError, match="at least one rule"):
        ValidateCode([])
