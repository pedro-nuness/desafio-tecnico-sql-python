"""Generic async circuit breaker: fail fast while a dependency is known to be down."""

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised instead of calling the dependency while the circuit is open."""

    def __init__(self, name: str, retry_after_seconds: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_after_seconds:.1f}s")
        self.name = name
        self.retry_after_seconds = retry_after_seconds


class CircuitBreaker:
    """CLOSED -> OPEN after `failure_threshold` consecutive failures.

    OPEN rejects calls until `reset_timeout_seconds` pass, then HALF_OPEN lets a single
    trial call through: success closes the circuit, failure reopens it. Only exceptions
    accepted by `is_failure` count as failures; anything else (bad requests, programming
    errors, cancellation) is neutral.

    Meant for one event loop: state is checked and updated with no await in between.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 60.0,
        is_failure: Callable[[Exception], bool] = lambda exc: True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_timeout_seconds <= 0:
            raise ValueError("reset_timeout_seconds must be > 0")
        self.name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._is_failure = is_failure
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

    async def call[T](self, func: Callable[[], Awaitable[T]]) -> T:
        self._before_call()
        # Needed: the breaker must observe the outcome to update its state, then re-raise.
        try:
            result = await func()
        except BaseException as exc:
            if isinstance(exc, Exception) and self._is_failure(exc):
                self._on_failure()
            else:
                self._trial_in_flight = False
            raise
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
