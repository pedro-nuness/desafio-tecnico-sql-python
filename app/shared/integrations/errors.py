from pydantic import JsonValue

from app.shared.errors import AppError


class IntegrationError(AppError):
    """An external dependency failed. HTTP 502, 503 (retry_after set) or 504 (timeout).

    Raised by `Integration` (translated SDK failures and adapter contract violations) and by
    application services that validate a dependency's answer.
    """

    def __init__(
        self,
        message: str,
        /,
        *,
        transient: bool = False,
        timeout: bool = False,
        retry_after: float | None = None,
        **payload: JsonValue,
    ) -> None:
        super().__init__(message, **payload)
        self.transient = transient
        """Worth retrying, and counts against the circuit breaker."""
        self.timeout = timeout
        self.retry_after = retry_after
        """Seconds until the dependency may be called again (open circuit)."""
