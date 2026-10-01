import ast
import asyncio

from app.features.modernization.domain.models.validation import (
    ValidationMessage,
    ValidationResult,
    ValidatorResult,
)


class PythonASTValidator:
    """Blocking check: the generated module must be syntactically valid Python."""

    name = "python_ast"

    async def validate(self, code: str) -> ValidationResult:
        (result,) = await asyncio.gather(
            asyncio.to_thread(self.check, code), return_exceptions=True
        )
        if isinstance(result, SyntaxError):
            result = ValidatorResult(
                validator=self.name,
                success=False,
                blocking=True,
                messages=(
                    ValidationMessage(
                        message=result.msg,
                        line=result.lineno,
                        column=result.offset,
                    ),
                ),
            )
        elif isinstance(result, BaseException):
            raise result
        return ValidationResult(results=(result,))

    def check(self, code: str) -> ValidatorResult:
        ast.parse(code, filename="generated_module.py", type_comments=False)
        return ValidatorResult(validator=self.name, success=True, blocking=True)
