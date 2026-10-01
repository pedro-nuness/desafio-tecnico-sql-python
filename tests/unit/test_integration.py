"""Integration: vendor-agnostic translation of SDK failures, retries and circuit breaker."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.integration import Integration


class _StatusError(Exception):
    """Any SDK error exposing `status_code` (OpenAI APIStatusError, OpenRouterError...)."""

    def __init__(self, status_code: int) -> None:
        super().__init__("secret upstream text")
        self.status_code = status_code


def _raising(exc: Exception) -> AsyncMock:
    return AsyncMock(side_effect=exc)


@pytest.mark.parametrize(
    ("status", "transient", "is_timeout"),
    [(400, False, False), (401, False, False), (408, True, True), (409, True, False),
     (429, True, False), (500, True, False), (504, True, True)],
)  # fmt: skip
async def test_status_codes_are_classified(status: int, transient: bool, is_timeout: bool) -> None:
    with pytest.raises(IntegrationError) as exc_info:
        await Integration("dep").call(_raising(_StatusError(status)))

    error = exc_info.value
    assert (error.transient, error.timeout) == (transient, is_timeout)
    assert error.payload == {"integration": "dep", "upstream_status": status}
    assert "secret" not in error.message


async def test_wrapped_timeout_and_transport_errors_are_found_in_the_cause_chain() -> None:
    class SdkTimeout(Exception): ...

    wrapped = SdkTimeout("sdk says timeout")
    wrapped.__cause__ = httpx.ReadTimeout("read timed out")
    with pytest.raises(IntegrationError) as timeout_info:
        await Integration("dep").call(_raising(wrapped))
    with pytest.raises(IntegrationError) as transport_info:
        await Integration("dep").call(_raising(httpx.ConnectError("refused")))

    assert timeout_info.value.timeout and timeout_info.value.transient
    assert transport_info.value.transient and not transport_info.value.timeout


async def test_unknown_failures_are_not_transient() -> None:
    func = _raising(RuntimeError("sdk bug"))
    with pytest.raises(IntegrationError, match="dep request failed") as exc_info:
        await Integration("dep", retries=3).call(func)

    assert not exc_info.value.transient
    assert func.await_count == 1


async def test_transient_failures_are_retried_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("app.shared.integrations.integration.asyncio.sleep", sleep)
    func = AsyncMock(side_effect=[_StatusError(503), _StatusError(503), "ok"])

    assert await Integration("dep", retries=2, backoff_seconds=0.1).call(func) == "ok"
    assert [call.args[0] for call in sleep.await_args_list] == [0.1, 0.2]


async def test_adapter_contract_errors_pass_through_untouched() -> None:
    integration = Integration("dep", retries=2)
    contract_error = integration.error("returned no choices", choices=0)

    with pytest.raises(IntegrationError) as exc_info:
        await integration.call(_raising(contract_error))

    assert exc_info.value is contract_error
    assert exc_info.value.message == "dep: returned no choices"
    assert exc_info.value.payload == {"integration": "dep", "choices": 0}


async def test_open_circuit_reports_retry_after_without_calling_the_dependency() -> None:
    integration = Integration("dep", failure_threshold=1, reset_timeout_seconds=30)
    func = _raising(_StatusError(503))
    with pytest.raises(IntegrationError):
        await integration.call(func)

    with pytest.raises(IntegrationError, match="unavailable") as exc_info:
        await integration.call(func)

    assert exc_info.value.retry_after == pytest.approx(30, abs=1)
    assert func.await_count == 1
