"""Selects the LLMProvider adapter from configuration (composition-time only).

Adding a vendor (e.g. AnthropicProvider) = one adapter module + one `case` here.
Nothing in graph/, application/ or domain/ changes.
"""

from app.application.ports.llm.llm_provider import LLMProvider
from app.config.settings import LLMProviderName, Settings
from app.domain.exceptions import ConfigurationError
from app.infrastructure.llm.openai_provider import OpenAIProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def create_llm_provider(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case LLMProviderName.OPENAI:
            return OpenAIProvider(
                api_key=_require_api_key(settings),
                model=settings.llm_model,
                base_url=settings.llm_base_url,
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                reasoning_effort=settings.llm_reasoning_effort,
            )
        case LLMProviderName.OPENROUTER:
            # OpenRouter speaks the OpenAI API: same adapter, different endpoint.
            return OpenAIProvider(
                api_key=_require_api_key(settings),
                model=settings.llm_model,
                provider_name="openrouter",
                base_url=settings.llm_base_url or OPENROUTER_BASE_URL,
                default_headers={"X-Title": settings.app_name},
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                reasoning_effort=settings.llm_reasoning_effort,
            )


def _require_api_key(settings: Settings) -> str:
    if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
        raise ConfigurationError(
            f"LLM_API_KEY is required for LLM_PROVIDER={settings.llm_provider}"
        )
    return settings.llm_api_key.get_secret_value()
