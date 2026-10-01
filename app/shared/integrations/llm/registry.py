"""Builds the declared providers: provider type -> adapter.

Adding a vendor = one adapter package + one entry in PROVIDER_TYPES. Each declared provider
gets its own Integration, so the circuit breaker is per endpoint: an outage skips every
route of that provider at once.
"""

from collections.abc import Callable, Mapping

from app.shared.errors import AppError
from app.shared.integrations.integration import Integration
from app.shared.integrations.llm.config import LLMSettings, ProviderSettings, ProviderType
from app.shared.integrations.llm.gateway import Provider
from app.shared.integrations.llm.openai.provider import OpenAIProvider
from app.shared.integrations.llm.openrouter.provider import OpenRouterProvider

type ProviderBuilder = Callable[[str, ProviderSettings, str, str | None], Provider]
"""(name, settings, api_key, app_name) -> Provider"""


def _openai(name: str, settings: ProviderSettings, api_key: str, app_name: str | None) -> Provider:
    return OpenAIProvider(
        name,
        api_key=api_key,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
        max_retries=settings.max_retries,  # the SDK retries (bounded)
        integration=_integration(name, settings, retries=0),
    )


def _openrouter(
    name: str, settings: ProviderSettings, api_key: str, app_name: str | None
) -> Provider:
    return OpenRouterProvider(
        name,
        api_key=api_key,
        app_name=app_name,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
        # The SDK only retries by elapsed time; the Integration bounds the attempts.
        integration=_integration(name, settings, retries=settings.max_retries),
    )


PROVIDER_TYPES: Mapping[ProviderType, ProviderBuilder] = {
    ProviderType.OPENAI: _openai,
    ProviderType.OPENROUTER: _openrouter,
}


def build_providers(settings: LLMSettings, *, app_name: str | None = None) -> dict[str, Provider]:
    """Builds the providers some route uses; fails fast when one of them has no API key."""
    used = {route.provider for route in settings.routes}
    providers: dict[str, Provider] = {}
    for name in sorted(used):
        provider_settings = settings.providers[name]
        if provider_settings.api_key is None or not provider_settings.api_key.get_secret_value():
            source = provider_settings.api_key_env or "LLM_API_KEY"
            raise AppError(f"LLM provider {name!r} has no API key: set {source}")
        build = PROVIDER_TYPES[provider_settings.type]
        providers[name] = build(
            name, provider_settings, provider_settings.api_key.get_secret_value(), app_name
        )
    return providers


def _integration(name: str, settings: ProviderSettings, *, retries: int) -> Integration:
    return Integration(
        name,
        retries=retries,
        failure_threshold=settings.circuit_breaker.failure_threshold,
        reset_timeout_seconds=settings.circuit_breaker.reset_seconds,
    )
