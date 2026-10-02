"""Boundary to one external dependency: retries, circuit breaker and error translation.

Instantiated once per dependency (factory / composition root) and handed to its adapter.
Native SDK exceptions never leave `call`: they become IntegrationError with a message we
write; the SDK text stays in `__cause__` (logged, never returned to clients).
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from pydantic import JsonValue

from app.shared.integrations.errors import IntegrationError
from app.shared.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError

_TIMEOUT_STATUSES = frozenset({408, 504})


class Integration:
    def __init__(
        self,
        name: str,
        *,
        retries: int = 0,
        backoff_seconds: float = 0.5,
        failure_threshold: int | None = None,
        reset_timeout_seconds: float = 60.0,
    ) -> None:
        """`retries`: extra attempts for transient failures (exponential backoff).
        `failure_threshold`: consecutive transient failures, after retries, that open the
        circuit; None disables the breaker."""
        if retries < 0:
            raise ValueError("retries must be >= 0")
        self.name = name
        self._retries = retries
        self._backoff_seconds = backoff_seconds
        self.breaker = (
            CircuitBreaker(
                name,
                failure_threshold=failure_threshold,
                reset_timeout_seconds=reset_timeout_seconds,
                is_failure=lambda exc: isinstance(exc, IntegrationError) and exc.transient,
            )
            if failure_threshold is not None
            else None
        )

    async def call[T](self, func: Callable[[], Awaitable[T]]) -> T:
        if self.breaker is None:
            return await self._attempts(func)
        # Needed: an open circuit is reported as this integration being unavailable.
        try:
            return await self.breaker.call(lambda: self._attempts(func))
        except CircuitOpenError as exc:
            raise IntegrationError(
                f"{self.name} is unavailable after repeated failures",
                retry_after=exc.retry_after_seconds,
                integration=self.name,
            ) from exc

    def error(self, message: str, /, **payload: JsonValue) -> IntegrationError:
        """The dependency answered, but outside its contract (not retried, not counted)."""
        return IntegrationError(f"{self.name}: {message}", integration=self.name, **payload)

    async def _attempts[T](self, func: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(self._retries + 1):
            # Needed: the single place where native SDK exceptions are translated.
            try:
                return await func()
            except IntegrationError as error:
                # Already ours: an adapter's transient error (a provider failing mid-answer)
                # is retried like a translated one; a contract error passes through.
                if not error.transient or attempt == self._retries:
                    raise
            except Exception as exc:
                error = self._translate(exc)
                if not error.transient or attempt == self._retries:
                    raise error from exc
            await asyncio.sleep(self._backoff_seconds * 2**attempt)
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _translate(self, exc: Exception) -> IntegrationError:
        """Vendor-agnostic: HTTP status when the SDK exposes one, else timeout/transport
        in the cause chain (SDKs wrap httpx errors), else a non-transient failure."""
        status = _status_code(exc)
        if status is not None:
            return IntegrationError(
                f"{self.name} answered HTTP {status}",
                transient=status in (409, 429) or status in _TIMEOUT_STATUSES or status >= 500,
                timeout=status in _TIMEOUT_STATUSES,
                integration=self.name,
                upstream_status=status,
            )
        if _caused_by(exc, (TimeoutError, httpx.TimeoutException)):
            return IntegrationError(
                f"{self.name} timed out", transient=True, timeout=True, integration=self.name
            )
        if _caused_by(exc, (ConnectionError, httpx.TransportError)):
            return IntegrationError(
                f"{self.name} is unreachable", transient=True, integration=self.name
            )
        return IntegrationError(f"{self.name} request failed", integration=self.name)


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:  # httpx.HTTPStatusError keeps it on the response
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _caused_by(exc: BaseException, types: tuple[type[BaseException], ...]) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, types):
            return True
        current = current.__cause__ or current.__context__
    return False
