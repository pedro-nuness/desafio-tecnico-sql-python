from typing import Protocol

from app.features.modernization.domain.models.validation import ValidationResult


class CodeValidator(Protocol):
    """Checks generated Python code. Async because some validators spawn processes.

    Native validation and execution exceptions propagate to the global HTTP handlers.
    """

    name: str

    async def validate(self, code: str) -> ValidationResult: ...
