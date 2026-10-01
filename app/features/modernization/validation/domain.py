from app.shared.domain.value_object import ValueObject


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
    skipped: str | None = None
    """Why the check could not run (e.g. no evaluation scenario for the routine). A skipped
    check passes, and the reason is reported as a warning."""


class ValidationResult(ValueObject):
    results: tuple[ValidatorResult, ...] = ()

    @property
    def is_valid(self) -> bool:
        """True when no blocking validator failed."""
        return all(result.success for result in self.results if result.blocking)

    @property
    def passed_all(self) -> bool:
        return all(result.success for result in self.results)

    def issues(self) -> tuple[str, ...]:
        """Every message of every failed validator, one line each (repair feedback)."""
        return tuple(
            line for result in self.results if not result.success for line in _lines(result)
        )

    def warnings(self) -> tuple[str, ...]:
        """Report lines: findings of non-blocking checks and checks that could not run."""
        return tuple(
            line
            for result in self.results
            if not result.success and not result.blocking
            for line in _lines(result)
        ) + tuple(
            f"[{result.validator}] not run: {result.skipped}"
            for result in self.results
            if result.skipped
        )


def _lines(result: ValidatorResult) -> tuple[str, ...]:
    return tuple(
        f"[{result.validator}] "
        + (f"L{message.line}: " if message.line else "")
        + (f"{message.code} " if message.code else "")
        + message.message
        for message in result.messages
    )
