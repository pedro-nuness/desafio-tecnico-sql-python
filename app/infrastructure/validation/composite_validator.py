import asyncio
from collections.abc import Sequence

from app.application.ports.validation.code_validator import CodeValidator
from app.domain.exceptions import ValidationExecutionError
from app.domain.models.validation import ValidationMessage, ValidationResult, ValidatorResult


class CompositeCodeValidator:
    """Runs independent validators concurrently and merges their results (Composite).

    A validator that cannot run does not hide the others: it is reported as a failed,
    non-blocking result, so the execution degrades to PARTIAL ("unverified").
    """

    name = "composite"

    def __init__(self, validators: Sequence[CodeValidator]) -> None:
        if not validators:
            raise ValueError("CompositeCodeValidator needs at least one validator")
        self._validators = tuple(validators)

    async def validate(self, code: str) -> ValidationResult:
        results = await asyncio.gather(*(self._run(v, code) for v in self._validators))
        merged = ValidationResult()
        for result in results:
            merged = merged.merge(result)
        return merged

    @staticmethod
    async def _run(validator: CodeValidator, code: str) -> ValidationResult:
        try:
            return await validator.validate(code)
        except ValidationExecutionError as exc:
            failure = ValidatorResult(
                validator=validator.name,
                success=False,
                blocking=False,
                messages=(ValidationMessage(message=f"validator did not run: {exc}"),),
            )
            return ValidationResult(results=(failure,))
