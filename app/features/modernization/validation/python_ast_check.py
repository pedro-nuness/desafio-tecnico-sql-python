import ast
import asyncio

from app.features.modernization.domain.validation import ValidationMessage


class PythonASTCheck:
    """The generated module must be syntactically valid Python."""

    name = "python_ast"

    async def check(self, code: str) -> tuple[ValidationMessage, ...]:
        return await asyncio.to_thread(_syntax_errors, code)


def _syntax_errors(code: str) -> tuple[ValidationMessage, ...]:
    # Needed: invalid syntax is a validation finding (it feeds the repair loop), not an error.
    try:
        ast.parse(code, filename="generated_module.py", type_comments=False)
    except SyntaxError as exc:
        return (ValidationMessage(message=exc.msg, line=exc.lineno, column=exc.offset),)
    return ()
