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
        return ValidationResult(results=(await asyncio.to_thread(self.check, code),))

    def check(self, code: str) -> ValidatorResult:
        # Needed: invalid syntax is a validation result (it feeds the repair loop), not an error.
        try:
            ast.parse(code, filename="generated_module.py", type_comments=False)
        except SyntaxError as exc:
            return ValidatorResult(
                validator=self.name,
                success=False,
                blocking=True,
                messages=(ValidationMessage(message=exc.msg, line=exc.lineno, column=exc.offset),),
            )
        return ValidatorResult(validator=self.name, success=True, blocking=True)
