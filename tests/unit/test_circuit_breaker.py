import asyncio

import pytest

from app.shared.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _ok() -> str:
    return "ok"


async def _boom() -> str:
    raise RuntimeError("boom")


def _breaker(clock: FakeClock, threshold: int = 2) -> CircuitBreaker:
    return CircuitBreaker("dep", failure_threshold=threshold, reset_timeout_seconds=30, clock=clock)


async def _fail(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)


async def test_opens_after_consecutive_failures_and_fails_fast() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)

    await _fail(breaker, 2)

    assert breaker.state is CircuitState.OPEN
    clock.now = 10
    with pytest.raises(CircuitOpenError, match=r"retry in 20.0s") as exc_info:
        await breaker.call(_ok)
    assert exc_info.value.name == "dep"
    assert exc_info.value.retry_after_seconds == 20


async def test_success_resets_the_failure_count() -> None:
    breaker = _breaker(FakeClock())

    await _fail(breaker, 1)
    assert await breaker.call(_ok) == "ok"
    await _fail(breaker, 1)

    assert breaker.state is CircuitState.CLOSED


async def test_half_open_trial_success_closes_the_circuit() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 2)

    clock.now = 30
    assert breaker.state is CircuitState.HALF_OPEN
    assert await breaker.call(_ok) == "ok"

    assert breaker.state is CircuitState.CLOSED


async def test_half_open_trial_failure_reopens_immediately() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, threshold=5)
    await _fail(breaker, 5)

    clock.now = 30
    await _fail(breaker, 1)

    assert breaker.state is CircuitState.OPEN


async def test_half_open_lets_a_single_trial_through() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 2)
    clock.now = 30
    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "ok"

    trial = asyncio.create_task(breaker.call(slow))
    await asyncio.sleep(0)
    with pytest.raises(CircuitOpenError):
        await breaker.call(_ok)
    release.set()

    assert await trial == "ok"
    assert breaker.state is CircuitState.CLOSED


async def test_exceptions_rejected_by_is_failure_are_neutral() -> None:
    breaker = CircuitBreaker(
        "dep", failure_threshold=1, is_failure=lambda exc: isinstance(exc, ValueError)
    )

    with pytest.raises(RuntimeError):
        await breaker.call(_boom)

    assert breaker.state is CircuitState.CLOSED


async def test_cancelling_half_open_trial_releases_the_probe() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker, 2)
    clock.now = 30
    started = asyncio.Event()

    async def slow() -> str:
        started.set()
        await asyncio.Event().wait()
        return "ok"

    trial = asyncio.create_task(breaker.call(slow))
    await started.wait()
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial
    assert await breaker.call(_ok) == "ok"
    assert breaker.state is CircuitState.CLOSED


async def test_exception_returned_as_data_is_not_a_failure() -> None:
    breaker = _breaker(FakeClock())
    value = RuntimeError("data")

    async def result() -> Exception:
        return value

    assert await breaker.call(result) is value
    assert breaker.state is CircuitState.CLOSED
