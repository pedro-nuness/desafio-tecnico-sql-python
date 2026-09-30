from app.domain.models.value_object import ValueObject


class ValidationMessage(ValueObject):
    message: str
    code: str | None = None
    line: int | None = None
    column: int | None = None


class ValidatorResult(ValueObject):
    validator: str
    success: bool
    blocking: bool
    """A failing blocking validator means the code is unusable (e.g. syntax error)."""
    messages: tuple[ValidationMessage, ...] = ()


class ValidationResult(ValueObject):
    results: tuple[ValidatorResult, ...] = ()

    @property
    def is_valid(self) -> bool:
        """True when no blocking validator failed."""
        return all(result.success for result in self.results if result.blocking)

    @property
    def passed_all(self) -> bool:
        return all(result.success for result in self.results)

    def merge(self, other: ValidationResult) -> ValidationResult:
        return ValidationResult(results=self.results + other.results)
