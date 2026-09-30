from typing import Protocol

from app.domain.models.validation import ValidationResult


class CodeValidator(Protocol):
    """Checks generated Python code. Async because some validators spawn processes.

    Implementations raise app.domain.exceptions.ValidationExecutionError when they
    cannot run; an invalid program is reported through ValidationResult instead.
    """

    name: str

    async def validate(self, code: str) -> ValidationResult: ...
