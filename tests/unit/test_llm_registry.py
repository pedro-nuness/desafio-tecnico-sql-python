"""Declared providers -> adapters, each with its own Integration (breaker per endpoint)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError, BadRequestError
from openrouter.errors import OpenRouterError
from pydantic import SecretStr

from app.shared.errors import AppError
from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.llm.config import (
    CircuitBreakerSettings,
    LLMSettings,
    ProviderSettings,
    ProviderType,
    Route,
)
from app.shared.integrations.llm.gateway import LLMGateway
from app.shared.integrations.llm.llm import LLMRequest
from app.shared.integrations.llm.openai.provider import OpenAIProvider
from app.shared.integrations.llm.openrouter.provider import (
    OPENROUTER_BASE_URL,
    OpenRouterProvider,
)
from app.shared.integrations.llm.registry import build_providers
from app.shared.resilience.circuit_breaker import CircuitState

REQUEST = LLMRequest(system_prompt="s", user_prompt="u")


def _provider(type_: ProviderType, *, key: str | None = "sk-test", **overrides) -> ProviderSettings:
    return ProviderSettings(
        type=type_, api_key=SecretStr(key) if key else None, api_key_env="KEY", **overrides
    )


def _settings(*routes: str, **providers: ProviderSettings) -> LLMSettings:
    return LLMSettings(
        providers=providers,
        routes=tuple(Route(provider=r.split("/")[0], model=r.split("/")[1]) for r in routes),
    )


def _completion() -> SimpleNamespace:
    return SimpleNamespace(
        model="served-model",
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, cost=None),
    )


def _stub_sdk(provider: object, monkeypatch: pytest.MonkeyPatch, sdk: AsyncMock) -> None:
    if isinstance(provider, OpenRouterProvider):
        monkeypatch.setattr(provider._client.chat, "send_async", sdk)
    else:
        assert isinstance(provider, OpenAIProvider)
        monkeypatch.setattr(provider._client.chat.completions, "create", sdk)


def _provider_down(provider: object) -> Exception:
    if isinstance(provider, OpenRouterProvider):
        return OpenRouterError("provider down", httpx.Response(500, text="provider down"))
    # The real SDK raises it `from` the httpx error, which is what classifies it.
    error = APIConnectionError(
        message="provider down", request=httpx.Request("POST", "https://example.com")
    )
    error.__cause__ = httpx.ConnectError("connection refused")
    return error


def test_each_type_builds_its_adapter_with_its_retry_ownership() -> None:
    settings = _settings(
        "or/m1",
        "oa/m2",
        **{
            "or": _provider(ProviderType.OPENROUTER, timeout_seconds=30, max_retries=3),
            "oa": _provider(ProviderType.OPENAI, max_retries=4),
        },
    )

    providers = build_providers(settings, app_name="app")

    openrouter, openai = providers["or"], providers["oa"]
    assert isinstance(openrouter, OpenRouterProvider)
    sdk = openrouter._client.sdk_configuration
    assert sdk.get_server_details()[0] == OPENROUTER_BASE_URL
    assert sdk.globals.x_open_router_title == "app"
    assert sdk.timeout_ms == 30_000
    assert sdk.retry_config is None  # time-based SDK retries off...
    assert openrouter._integration._retries == 3  # ...bounded retries in the Integration
    assert isinstance(openai, OpenAIProvider)
    assert openai._client.max_retries == 4  # the OpenAI SDK retries itself
    assert openai._integration._retries == 0


def test_breaker_settings_are_per_provider() -> None:
    breaker = CircuitBreakerSettings(failure_threshold=3, reset_seconds=10)
    settings = _settings(
        "or/m1", **{"or": _provider(ProviderType.OPENROUTER, circuit_breaker=breaker)}
    )

    provider = build_providers(settings)["or"]

    assert isinstance(provider, OpenRouterProvider)
    integration = provider._integration
    assert (integration.name, integration.breaker is not None) == ("or", True)
    assert integration.breaker._failure_threshold == 3
    assert integration.breaker._reset_timeout == 10


def test_only_providers_used_by_a_route_are_built() -> None:
    settings = _settings(
        "or/m1",
        **{
            "or": _provider(ProviderType.OPENROUTER),
            "unused": _provider(ProviderType.OPENAI, key=None),  # no key, but never used
        },
    )

    assert set(build_providers(settings)) == {"or"}


def test_a_used_provider_without_key_fails_at_composition() -> None:
    settings = _settings("or/m1", **{"or": _provider(ProviderType.OPENROUTER, key=None)})

    with pytest.raises(AppError, match="'or' has no API key: set KEY"):
        build_providers(settings)


@pytest.mark.parametrize("provider_type", list(ProviderType))
async def test_an_outage_opens_the_provider_circuit_for_all_its_routes(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaker = CircuitBreakerSettings(failure_threshold=1, reset_seconds=60)
    settings = _settings(
        "p/m1", "p/m2", p=_provider(provider_type, max_retries=0, circuit_breaker=breaker)
    )
    providers = build_providers(settings)
    provider = providers["p"]
    sdk_error = _provider_down(provider)
    sdk = AsyncMock(side_effect=[sdk_error, _completion()])
    _stub_sdk(provider, monkeypatch, sdk)

    with pytest.raises(IntegrationError, match="every route failed") as exc_info:
        await LLMGateway(providers, settings.routes).generate(REQUEST)

    # m1 hit the outage; m2 was refused by the open circuit without calling the SDK.
    assert sdk.await_count == 1
    first, second = exc_info.value.payload["attempts"]
    assert "provider down" not in first["error"]
    assert "unavailable" in second["error"]
    assert exc_info.value.retry_after is None  # m1 failed for real: not every circuit was open
    assert provider._integration.breaker.state is CircuitState.OPEN


@pytest.mark.parametrize("provider_type", list(ProviderType))
async def test_caller_errors_do_not_open_the_circuit(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaker = CircuitBreakerSettings(failure_threshold=1)
    settings = _settings("p/m1", p=_provider(provider_type, max_retries=0, circuit_breaker=breaker))
    provider = build_providers(settings)["p"]
    bad_request = httpx.Response(400, request=httpx.Request("POST", "https://example.com"))
    sdk_error = (
        OpenRouterError("prompt too long", bad_request)
        if isinstance(provider, OpenRouterProvider)
        else BadRequestError("prompt too long", response=bad_request, body=None)
    )
    sdk = AsyncMock(side_effect=sdk_error)
    _stub_sdk(provider, monkeypatch, sdk)

    for _ in range(3):
        with pytest.raises(IntegrationError) as exc_info:
            await provider.complete(REQUEST, Route(provider="p", model="m1"))
        assert exc_info.value.payload["upstream_status"] == 400

    assert provider._integration.breaker.state is CircuitState.CLOSED
    assert sdk.await_count == 3


@pytest.mark.parametrize("provider_type", list(ProviderType))
async def test_route_parameters_reach_the_sdk(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = build_providers(_settings("p/m1", p=_provider(provider_type)))["p"]
    sdk = AsyncMock(return_value=_completion())
    _stub_sdk(provider, monkeypatch, sdk)

    response = await provider.complete(
        REQUEST, Route(provider="p", model="m1", reasoning_effort="low")
    )

    assert sdk.await_args is not None
    assert (sdk.await_args.kwargs["model"], sdk.await_args.kwargs["reasoning_effort"]) == (
        "m1",
        "low",
    )
    assert (response.provider, response.model, response.content) == ("p", "served-model", "ok")


@pytest.mark.parametrize("provider_type", list(ProviderType))
async def test_empty_choices_break_the_contract_without_opening_the_circuit(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaker = CircuitBreakerSettings(failure_threshold=1)
    provider = build_providers(
        _settings("p/m1", p=_provider(provider_type, circuit_breaker=breaker))
    )["p"]
    _stub_sdk(provider, monkeypatch, AsyncMock(return_value=SimpleNamespace(choices=[])))

    with pytest.raises(IntegrationError, match="returned no choices") as exc_info:
        await provider.complete(REQUEST, Route(provider="p", model="m1"))

    assert not exc_info.value.transient
    assert exc_info.value.payload == {"integration": "p", "model": "m1"}
    assert provider._integration.breaker.state is CircuitState.CLOSED
