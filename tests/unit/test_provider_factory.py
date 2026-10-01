from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError, BadRequestError
from openrouter import OpenRouter
from openrouter.errors import OpenRouterError
from pydantic import SecretStr, ValidationError

from app.core.config.settings import Settings
from app.shared.errors import AppError
from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.integration import Integration
from app.shared.integrations.llm.config import LLMConfig, LLMProviderName
from app.shared.integrations.llm.factory import create_llm_provider
from app.shared.integrations.llm.llm_provider import LLMRequest
from app.shared.integrations.llm.openai.provider import OpenAIProvider
from app.shared.integrations.llm.openrouter.provider import (
    OPENROUTER_BASE_URL,
    OpenRouterProvider,
)
from app.shared.resilience.circuit_breaker import CircuitState

type Provider = OpenAIProvider | OpenRouterProvider


def _config(provider: LLMProviderName, api_key: str | None = "sk-test", **overrides) -> LLMConfig:
    return LLMConfig(
        provider=provider,
        model="some/model",
        api_key=SecretStr(api_key) if api_key else None,
        **overrides,
    )


def _completion() -> SimpleNamespace:
    return SimpleNamespace(
        model="some/model",
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )


def _stub_sdk(provider: Provider, monkeypatch: pytest.MonkeyPatch, sdk: AsyncMock) -> None:
    if isinstance(provider, OpenRouterProvider):
        monkeypatch.setattr(provider._client.chat, "send_async", sdk)
    else:
        monkeypatch.setattr(provider._client.chat.completions, "create", sdk)


def _provider_down(provider: Provider) -> Exception:
    if isinstance(provider, OpenRouterProvider):
        return OpenRouterError("provider down", httpx.Response(500, text="provider down"))
    # The real SDK raises it `from` the httpx error, which is what classifies it.
    error = APIConnectionError(
        message="provider down", request=httpx.Request("POST", "https://example.com")
    )
    error.__cause__ = httpx.ConnectError("connection refused")
    return error


def test_unsupported_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="fake", _env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("provider", [LLMProviderName.OPENAI, LLMProviderName.OPENROUTER])
def test_real_providers_require_api_key(provider: LLMProviderName) -> None:
    with pytest.raises(AppError, match="LLM_API_KEY"):
        create_llm_provider(_config(provider, None))


def test_openrouter_uses_its_official_sdk() -> None:
    provider = create_llm_provider(_config(LLMProviderName.OPENROUTER, app_name="app"))

    assert isinstance(provider, OpenRouterProvider)
    assert not isinstance(provider, OpenAIProvider)
    assert isinstance(provider._client, OpenRouter)
    config = provider._client.sdk_configuration
    assert config.get_server_details()[0] == OPENROUTER_BASE_URL
    assert config.globals.x_open_router_title == "app"
    assert config.timeout_ms == 120_000
    assert config.retry_config is None  # retries belong to the Integration


@pytest.mark.parametrize("provider_name", list(LLMProviderName))
def test_reasoning_effort_reaches_the_adapter(provider_name: LLMProviderName) -> None:
    provider = create_llm_provider(_config(provider_name, reasoning_effort="low"))

    assert isinstance(provider, (OpenAIProvider, OpenRouterProvider))
    assert provider._reasoning_effort == "low"


@pytest.mark.parametrize(
    ("provider_name", "integration_retries"),
    [(LLMProviderName.OPENAI, 0), (LLMProviderName.OPENROUTER, 4)],
)
def test_settings_reach_the_integration(
    provider_name: LLMProviderName, integration_retries: int
) -> None:
    settings = Settings(
        llm_provider=provider_name,
        llm_api_key=SecretStr("sk-test"),
        llm_max_retries=4,
        llm_circuit_breaker_failure_threshold=3,
        llm_circuit_breaker_reset_seconds=10,
        _env_file=None,  # type: ignore[call-arg]
    )

    provider = create_llm_provider(settings.llm_config())

    assert isinstance(provider, (OpenAIProvider, OpenRouterProvider))
    integration = provider._integration
    assert integration.name == provider_name.value
    # OpenAI retries inside its SDK; OpenRouter's SDK has no bounded retry, so we do.
    assert integration._retries == integration_retries
    assert integration.breaker is not None
    assert integration.breaker._failure_threshold == 3
    assert integration.breaker._reset_timeout == 10


@pytest.mark.parametrize("provider_name", list(LLMProviderName))
async def test_provider_blocks_sdk_calls_until_circuit_recovers(
    provider_name: LLMProviderName, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = create_llm_provider(
        _config(provider_name, circuit_breaker_failure_threshold=2, max_retries=0)
    )
    assert isinstance(provider, (OpenAIProvider, OpenRouterProvider))
    breaker = provider._integration.breaker
    assert breaker is not None
    now = 0.0
    monkeypatch.setattr(breaker, "_clock", lambda: now)
    sdk_error = _provider_down(provider)
    sdk = AsyncMock(side_effect=[sdk_error, sdk_error, _completion()])
    _stub_sdk(provider, monkeypatch, sdk)
    request = LLMRequest(system_prompt="s", user_prompt="u")

    async with provider._client:
        for _ in range(2):
            with pytest.raises(IntegrationError) as exc_info:
                await provider.generate(request)
            assert exc_info.value.transient
            assert exc_info.value.__cause__ is sdk_error
            assert "provider down" not in exc_info.value.message
        assert breaker.state is CircuitState.OPEN
        with pytest.raises(IntegrationError, match="unavailable") as exc_info:
            await provider.generate(request)
        assert exc_info.value.retry_after == 60
        assert exc_info.value.payload == {"integration": provider_name.value}
        assert sdk.await_count == 2

        now = 60
        response = await provider.generate(request)
        assert response.content == "ok"
        assert response.provider == provider_name.value
        assert (response.input_tokens, response.output_tokens, response.finish_reason) == (
            2,
            1,
            "stop",
        )
        assert breaker.state is CircuitState.CLOSED
        assert sdk.await_count == 3


@pytest.mark.parametrize("provider_type", [OpenAIProvider, OpenRouterProvider])
async def test_provider_without_explicit_integration_can_generate(
    provider_type: type[OpenAIProvider] | type[OpenRouterProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = provider_type(api_key="sk-test", model="some/model")
    sdk = AsyncMock(return_value=_completion())
    _stub_sdk(provider, monkeypatch, sdk)
    async with provider._client:
        assert provider._integration.breaker is None
        response = await provider.generate(LLMRequest(system_prompt="s", user_prompt="u"))
        assert response.content == "ok"
        sdk.assert_awaited_once()


@pytest.mark.parametrize("provider_type", [OpenAIProvider, OpenRouterProvider])
async def test_empty_choices_break_the_contract_without_opening_the_circuit(
    provider_type: type[OpenAIProvider] | type[OpenRouterProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration = Integration("llm", failure_threshold=1)
    provider = provider_type(api_key="sk-test", model="some/model", integration=integration)
    _stub_sdk(provider, monkeypatch, AsyncMock(return_value=SimpleNamespace(choices=[])))
    async with provider._client:
        with pytest.raises(IntegrationError, match="returned no choices") as exc_info:
            await provider.generate(LLMRequest(system_prompt="s", user_prompt="u"))
    assert not exc_info.value.transient
    assert integration.breaker is not None
    assert integration.breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize("provider_name", list(LLMProviderName))
async def test_caller_errors_do_not_open_the_circuit(
    provider_name: LLMProviderName, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = create_llm_provider(
        _config(provider_name, circuit_breaker_failure_threshold=1, max_retries=0)
    )
    assert isinstance(provider, (OpenAIProvider, OpenRouterProvider))
    bad_request = httpx.Response(400, request=httpx.Request("POST", "https://example.com"))
    sdk_error = (
        OpenRouterError("prompt too long", bad_request)
        if isinstance(provider, OpenRouterProvider)
        else BadRequestError("prompt too long", response=bad_request, body=None)
    )
    sdk = AsyncMock(side_effect=sdk_error)
    _stub_sdk(provider, monkeypatch, sdk)

    async with provider._client:
        for _ in range(3):
            with pytest.raises(IntegrationError) as exc_info:
                await provider.generate(LLMRequest(system_prompt="s", user_prompt="u"))
            assert exc_info.value.payload["upstream_status"] == 400
            assert not exc_info.value.transient

    assert provider._integration.breaker is not None
    assert provider._integration.breaker.state is CircuitState.CLOSED
    assert sdk.await_count == 3
