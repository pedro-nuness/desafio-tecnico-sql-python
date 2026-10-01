from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIError
from openrouter import OpenRouter
from openrouter.errors import OpenRouterError
from pydantic import SecretStr, ValidationError

from app.core.config.settings import Settings
from app.shared.integrations.exceptions import IntegrationConfigurationError, IntegrationError
from app.shared.integrations.llm.config import LLMConfig, LLMProviderName
from app.shared.integrations.llm.factory import create_llm_provider
from app.shared.integrations.llm.llm_provider import LLMRequest
from app.shared.integrations.llm.openai.provider import OpenAIProvider
from app.shared.integrations.llm.openrouter.provider import (
    OPENROUTER_BASE_URL,
    OpenRouterProvider,
)
from app.shared.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def _config(provider: LLMProviderName, api_key: str | None = "sk-test", **overrides) -> LLMConfig:
    return LLMConfig(
        provider=provider,
        model="some/model",
        api_key=SecretStr(api_key) if api_key else None,
        **overrides,
    )


def test_unsupported_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="fake", _env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("provider", [LLMProviderName.OPENAI, LLMProviderName.OPENROUTER])
def test_real_providers_require_api_key(provider: LLMProviderName) -> None:
    with pytest.raises(IntegrationConfigurationError, match="LLM_API_KEY"):
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
    assert config.retry_config is None


@pytest.mark.parametrize("provider_name", list(LLMProviderName))
def test_reasoning_effort_reaches_the_adapter(provider_name: LLMProviderName) -> None:
    provider = create_llm_provider(_config(provider_name, reasoning_effort="low"))

    assert isinstance(provider, (OpenAIProvider, OpenRouterProvider))
    assert provider._reasoning_effort == "low"


@pytest.mark.parametrize("provider_name", list(LLMProviderName))
def test_breaker_settings_reach_the_provider(provider_name: LLMProviderName) -> None:
    settings = Settings(
        llm_provider=provider_name,
        llm_api_key=SecretStr("sk-test"),
        llm_circuit_breaker_failure_threshold=3,
        llm_circuit_breaker_reset_seconds=10,
        _env_file=None,  # type: ignore[call-arg]
    )

    provider = create_llm_provider(settings.llm_config())

    assert isinstance(provider, (OpenAIProvider, OpenRouterProvider))
    assert provider._breaker is not None
    assert provider._breaker._failure_threshold == 3
    assert provider._breaker._reset_timeout == 10
    assert provider._breaker._failure_types == (IntegrationError,)


def _completion() -> SimpleNamespace:
    return SimpleNamespace(
        model="some/model",
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )


@pytest.mark.parametrize("provider_name", list(LLMProviderName))
async def test_provider_blocks_sdk_calls_until_circuit_recovers(
    provider_name: LLMProviderName, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = create_llm_provider(
        _config(provider_name, circuit_breaker_failure_threshold=2, max_retries=0)
    )
    assert isinstance(provider, (OpenAIProvider, OpenRouterProvider))
    breaker = provider._breaker
    assert breaker is not None
    now = 0.0
    monkeypatch.setattr(breaker, "_clock", lambda: now)
    sdk_error = (
        OpenRouterError("provider down", httpx.Response(500, text="provider down"))
        if isinstance(provider, OpenRouterProvider)
        else APIError("provider down", httpx.Request("POST", "https://example.com"), body=None)
    )
    create = AsyncMock(side_effect=[sdk_error, sdk_error, _completion()])
    if isinstance(provider, OpenRouterProvider):
        monkeypatch.setattr(provider._client.chat, "send_async", create)
    else:
        monkeypatch.setattr(provider._client.chat.completions, "create", create)
    request = LLMRequest(system_prompt="s", user_prompt="u")

    async with provider._client:
        for _ in range(2):
            with pytest.raises(type(sdk_error), match="provider down") as exc_info:
                await provider.generate(request)
            assert exc_info.value is sdk_error
        assert breaker.state is CircuitState.OPEN
        with pytest.raises(CircuitOpenError) as exc_info:
            await provider.generate(request)
        assert exc_info.value.name == f"llm:{provider_name}"
        assert exc_info.value.retry_after_seconds == 60
        assert create.await_count == 2

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
        assert create.await_count == 3


@pytest.mark.parametrize("provider_type", [OpenAIProvider, OpenRouterProvider])
async def test_provider_without_breaker_can_generate(
    provider_type: type[OpenAIProvider] | type[OpenRouterProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = provider_type(api_key="sk-test", model="some/model")
    create = AsyncMock(return_value=_completion())
    if isinstance(provider, OpenRouterProvider):
        monkeypatch.setattr(provider._client.chat, "send_async", create)
    else:
        monkeypatch.setattr(provider._client.chat.completions, "create", create)
    async with provider._client:
        assert provider._breaker is None
        response = await provider.generate(LLMRequest(system_prompt="s", user_prompt="u"))
        assert response.content == "ok"
        create.assert_awaited_once()


@pytest.mark.parametrize("provider_type", [OpenAIProvider, OpenRouterProvider])
async def test_empty_sdk_choices_count_as_integration_failure(
    provider_type: type[OpenAIProvider] | type[OpenRouterProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = CircuitBreaker("llm", failure_threshold=1, failure_types=(IntegrationError,))
    provider = provider_type(api_key="sk-test", model="some/model", breaker=breaker)
    create = AsyncMock(return_value=SimpleNamespace(choices=[]))
    if isinstance(provider, OpenRouterProvider):
        monkeypatch.setattr(provider._client.chat, "send_async", create)
    else:
        monkeypatch.setattr(provider._client.chat.completions, "create", create)
    async with provider._client:
        with pytest.raises(IntegrationError, match="returned no choices"):
            await provider.generate(LLMRequest(system_prompt="s", user_prompt="u"))
        assert breaker.state is CircuitState.OPEN
