"""LLM configuration: declared providers (endpoints) and the routes tried, in order.

Loaded from YAML (see config/llm.example.yml) or, without a file, built from the single
provider described by the LLM_* environment variables. Secrets never live in the file:
each provider names the environment variable holding its API key.
"""

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.shared.errors import AppError

type ReasoningEffort = Literal["minimal", "low", "medium", "high"]


class ProviderType(StrEnum):
    OPENAI = "openai"
    OPENROUTER = "openrouter"


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CircuitBreakerSettings(_Config):
    failure_threshold: int = Field(default=5, ge=1)
    """Consecutive transient failures (after retries) that open the circuit."""
    reset_seconds: float = Field(default=60.0, gt=0)


class ProviderSettings(_Config):
    """One endpoint: credentials, transport and resilience, shared by all its routes."""

    type: ProviderType
    api_key_env: str | None = None
    """Name of the environment variable holding the key (never the key itself)."""
    api_key: SecretStr | None = Field(default=None, exclude=True)
    """Resolved from api_key_env at load time; rejected when written in the file."""
    base_url: str | None = None
    """OpenAI: any compatible API; OpenRouter: its server URL."""
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    circuit_breaker: CircuitBreakerSettings = CircuitBreakerSettings()


class Route(_Config):
    """One model on one declared provider."""

    provider: str
    model: str
    reasoning_effort: ReasoningEffort | None = None
    """Reasoning models only; unset = provider default (not sent)."""

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


class LLMSettings(_Config):
    providers: dict[str, ProviderSettings] = Field(min_length=1)
    routes: tuple[Route, ...] = Field(min_length=1)
    """Tried in priority order until one answers."""
    budget_seconds: float | None = Field(default=None, gt=0)
    """No further route starts once this much time has passed (synchronous requests)."""

    @model_validator(mode="after")
    def _routes_use_declared_providers(self) -> Self:
        for route in self.routes:
            if route.provider not in self.providers:
                raise ValueError(
                    f"route {route.label!r} uses undeclared provider {route.provider!r} "
                    f"(declared: {sorted(self.providers)})"
                )
        return self

    @classmethod
    def from_yaml(cls, path: Path, *, read_env: Callable[[str], str | None]) -> Self:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, provider in (raw.get("providers") or {}).items():
            if isinstance(provider, dict) and "api_key" in provider:
                raise AppError(
                    f"{path}: provider {name!r} has an inline api_key; "
                    "use api_key_env with the name of an environment variable"
                )
        settings = cls.model_validate(raw)
        return settings.model_copy(
            update={
                "providers": {
                    name: provider.model_copy(
                        update={"api_key": _secret(read_env(provider.api_key_env))}
                    )
                    if provider.api_key_env
                    else provider
                    for name, provider in settings.providers.items()
                }
            }
        )


def _secret(value: str | None) -> SecretStr | None:
    return SecretStr(value) if value else None
