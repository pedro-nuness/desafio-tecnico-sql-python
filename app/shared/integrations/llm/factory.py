"""Selects the LLMProvider adapter from configuration (composition-time only).

Adding a vendor (e.g. AnthropicProvider) = one adapter package + one `case` here.
Every provider receives a circuit breaker; callers only see the LLMProvider port.
"""

from app.shared.integrations.exceptions import IntegrationConfigurationError, IntegrationError
from app.shared.integrations.llm.config import LLMConfig, LLMProviderName
from app.shared.integrations.llm.llm_provider import LLMProvider
from app.shared.integrations.llm.openai.provider import OpenAIProvider
from app.shared.integrations.llm.openrouter.provider import OpenRouterProvider
from app.shared.resilience.circuit_breaker import CircuitBreaker


def create_llm_provider(config: LLMConfig) -> LLMProvider:
    api_key = _require_api_key(config)
    breaker = CircuitBreaker(
        f"llm:{config.provider}",
        failure_threshold=config.circuit_breaker_failure_threshold,
        reset_timeout_seconds=config.circuit_breaker_reset_seconds,
        failure_types=(IntegrationError,),
    )
    provider: LLMProvider
    match config.provider:
        case LLMProviderName.OPENAI:
            provider = OpenAIProvider(
                api_key=api_key,
                model=config.model,
                base_url=config.base_url,
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,
                reasoning_effort=config.reasoning_effort,
                breaker=breaker,
            )
        case LLMProviderName.OPENROUTER:
            provider = OpenRouterProvider(
                api_key=api_key,
                model=config.model,
                app_name=config.app_name,
                base_url=config.base_url,
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,
                reasoning_effort=config.reasoning_effort,
                breaker=breaker,
            )
    return provider


def _require_api_key(config: LLMConfig) -> str:
    if config.api_key is None or not config.api_key.get_secret_value():
        raise IntegrationConfigurationError(
            f"LLM_API_KEY is required for LLM_PROVIDER={config.provider}"
        )
    return config.api_key.get_secret_value()
