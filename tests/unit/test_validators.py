from app.features.modernization.domain.exceptions import ValidationExecutionError
from app.features.modernization.domain.models.validation import ValidationResult
from app.features.modernization.infrastructure.validation.composite_validator import (
    CompositeCodeValidator,
)
from app.features.modernization.infrastructure.validation.python_ast_validator import (
    PythonASTValidator,
)
from app.features.modernization.infrastructure.validation.ruff_validator import (
    RuffValidator,
)
from tests.conftest import VALID_CODE


async def test_ast_validator_accepts_valid_python() -> None:
    result = await PythonASTValidator().validate(VALID_CODE)

    assert result.is_valid and result.passed_all
    assert result.results[0].validator == "python_ast"


async def test_ast_validator_rejects_syntax_errors_as_blocking() -> None:
    result = await PythonASTValidator().validate("def broken(:\n    pass\n")

    [check] = result.results
    assert not check.success and check.blocking
    assert check.messages[0].line == 1
    assert not result.is_valid


async def test_ast_validator_accepts_python_314_syntax() -> None:
    code = "def first[T](items: list[T]) -> T:\n    return items[0]\n"
    assert (await PythonASTValidator().validate(code)).is_valid


async def test_ruff_reports_findings_as_non_blocking() -> None:
    result = await RuffValidator().validate("import os\n\n\ndef f():\n    return undefined_name\n")

    [check] = result.results
    assert not check.success and not check.blocking
    assert {"F401", "F821"} <= {m.code for m in check.messages}
    assert result.is_valid and not result.passed_all


async def test_ruff_flags_sql_built_with_string_formatting() -> None:
    code = 'def q(table: str) -> str:\n    return f"SELECT * FROM {table} WHERE id = 1"\n'
    result = await RuffValidator().validate(code)

    assert "S608" in {m.code for m in result.results[0].messages}


async def test_ruff_accepts_clean_code() -> None:
    assert (await RuffValidator().validate(VALID_CODE)).passed_all


class _BrokenValidator:
    name = "broken"

    async def validate(self, code: str) -> ValidationResult:
        raise ValidationExecutionError("binary missing")


async def test_composite_merges_results_and_degrades_validator_crashes() -> None:
    composite = CompositeCodeValidator([PythonASTValidator(), _BrokenValidator()])

    result = await composite.validate(VALID_CODE)

    assert [r.validator for r in result.results] == ["python_ast", "broken"]
    assert result.is_valid  # the crash is non-blocking...
    assert not result.passed_all  # ...but the code is not fully verified
