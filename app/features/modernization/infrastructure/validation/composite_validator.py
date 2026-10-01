import asyncio
from collections.abc import Sequence

from app.features.modernization.application.ports.validation.code_validator import CodeValidator
from app.features.modernization.domain.models.validation import (
    ValidationResult,
)


class CompositeCodeValidator:
    """Runs validators concurrently, merges their results, and propagates exceptions."""

    name = "composite"

    def __init__(self, validators: Sequence[CodeValidator]) -> None:
        if not validators:
            raise ValueError("CompositeCodeValidator needs at least one validator")
        self._validators = tuple(validators)

    async def validate(self, code: str) -> ValidationResult:
        results = await asyncio.gather(*(v.validate(code) for v in self._validators))
        merged = ValidationResult()
        for result in results:
            merged = merged.merge(result)
        return merged
