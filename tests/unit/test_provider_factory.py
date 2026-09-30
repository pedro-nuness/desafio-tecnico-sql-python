import pytest
from pydantic import SecretStr, ValidationError

from app.config.settings import LLMProviderName, Settings
from app.domain.exceptions import ConfigurationError
from app.infrastructure.llm.openai_provider import OpenAIProvider
from app.infrastructure.llm.provider_factory import OPENROUTER_BASE_URL, create_llm_provider


def _settings(provider: LLMProviderName, api_key: str | None = "sk-test") -> Settings:
    return Settings(
        llm_provider=provider,
        llm_model="some/model",
        llm_api_key=SecretStr(api_key) if api_key else None,
        _env_file=None,  # type: ignore[call-arg]
    )


def test_unsupported_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_provider="fake", _env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("provider", [LLMProviderName.OPENAI, LLMProviderName.OPENROUTER])
def test_real_providers_require_api_key(provider: LLMProviderName) -> None:
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        create_llm_provider(_settings(provider, None))


def test_openrouter_reuses_openai_compatible_adapter() -> None:
    provider = create_llm_provider(_settings(LLMProviderName.OPENROUTER))

    assert isinstance(provider, OpenAIProvider)
    assert str(provider._client.base_url).rstrip("/") == OPENROUTER_BASE_URL


def test_reasoning_effort_reaches_the_adapter() -> None:
    settings = _settings(LLMProviderName.OPENROUTER).model_copy(
        update={"llm_reasoning_effort": "low"}
    )

    provider = create_llm_provider(settings)

    assert isinstance(provider, OpenAIProvider)
    assert provider._reasoning_effort == "low"
