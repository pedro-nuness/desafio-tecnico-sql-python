"""Generic async circuit breaker: fail fast while a dependency is known to be down."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum

from app.shared.integrations.exceptions import IntegrationError


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(IntegrationError):
    """Raised instead of calling the dependency while the circuit is open."""

    def __init__(self, name: str, retry_after_seconds: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_after_seconds:.1f}s")
        self.name = name
        self.retry_after_seconds = retry_after_seconds


class CircuitBreaker:
    """CLOSED -> OPEN after `failure_threshold` consecutive failures.

    OPEN rejects calls until `reset_timeout_seconds` pass, then HALF_OPEN lets a single
    trial call through: success closes the circuit, failure reopens it. Only exceptions
    in `failure_types` count as failures; anything else (e.g. cancellation) is neutral.

    Meant for one event loop: state is checked and updated with no await in between.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 60.0,
        failure_types: tuple[type[BaseException], ...] = (Exception,),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_timeout_seconds <= 0:
            raise ValueError("reset_timeout_seconds must be > 0")
        self.name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._failure_types = failure_types
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self._reset_timeout:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    async def call[T](
        self,
        func: Callable[[], Awaitable[T]],
        *,
        failure_types: tuple[type[BaseException], ...] = (),
    ) -> T:
        self._before_call()

        async def invoke() -> T:
            return await func()

        task = asyncio.create_task(invoke())
        try:
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._trial_in_flight = False
        error = None if task.cancelled() else task.exception()
        if isinstance(error, (*self._failure_types, *failure_types)):
            self._on_failure()
        if error is not None:
            raise error
        result = task.result()
        self._on_success()
        return result

    def _before_call(self) -> None:
        state = self.state
        if state is CircuitState.CLOSED:
            return
        if state is CircuitState.HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return
        raise CircuitOpenError(self.name, self._retry_after())

    def _retry_after(self) -> float:
        opened_at = self._opened_at if self._opened_at is not None else self._clock()
        return max(0.0, opened_at + self._reset_timeout - self._clock())

    def _on_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def _on_failure(self) -> None:
        self._failures += 1
        if self._trial_in_flight or self._failures >= self._failure_threshold:
            self._opened_at = self._clock()
        self._trial_in_flight = False
