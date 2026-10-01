"""Selects the LLMProvider adapter from configuration (composition-time only).

Adding a vendor (e.g. AnthropicProvider) = one adapter package + one `case` here.
Every provider gets its own Integration (error translation + circuit breaker, plus
retries when its SDK has no bounded retry of its own); callers only see the port.
"""

from app.shared.errors import AppError
from app.shared.integrations.integration import Integration
from app.shared.integrations.llm.config import LLMConfig, LLMProviderName
from app.shared.integrations.llm.llm_provider import LLMProvider
from app.shared.integrations.llm.openai.provider import OpenAIProvider
from app.shared.integrations.llm.openrouter.provider import OpenRouterProvider


def create_llm_provider(config: LLMConfig) -> LLMProvider:
    api_key = _require_api_key(config)
    match config.provider:
        case LLMProviderName.OPENAI:
            return OpenAIProvider(
                api_key=api_key,
                model=config.model,
                base_url=config.base_url,
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,  # the SDK retries
                reasoning_effort=config.reasoning_effort,
                integration=_integration(config, retries=0),
            )
        case LLMProviderName.OPENROUTER:
            return OpenRouterProvider(
                api_key=api_key,
                model=config.model,
                app_name=config.app_name,
                base_url=config.base_url,
                timeout_seconds=config.timeout_seconds,
                reasoning_effort=config.reasoning_effort,
                integration=_integration(config, retries=config.max_retries),
            )


def _integration(config: LLMConfig, *, retries: int) -> Integration:
    return Integration(
        config.provider.value,
        retries=retries,
        failure_threshold=config.circuit_breaker_failure_threshold,
        reset_timeout_seconds=config.circuit_breaker_reset_seconds,
    )


def _require_api_key(config: LLMConfig) -> str:
    if config.api_key is None or not config.api_key.get_secret_value():
        raise AppError(f"LLM_API_KEY is required for LLM_PROVIDER={config.provider}")
    return config.api_key.get_secret_value()
