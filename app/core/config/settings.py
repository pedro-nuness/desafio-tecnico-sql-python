import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.shared.integrations.llm.config import (
    CircuitBreakerSettings,
    LLMSettings,
    ProviderSettings,
    ProviderType,
    ReasoningEffort,
    Route,
)


class Settings(BaseSettings):
    """Single source of configuration (environment variables / .env)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "plpgsql-modernizer"
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://modernizer:modernizer@localhost:5432/modernizer"
    )
    database_echo: bool = False

    llm_config_file: Path | None = None
    """YAML with the declared providers and the routes tried in order
    (config/llm.example.yml). When unset, the LLM_* variables below describe one route."""
    llm_provider: ProviderType = ProviderType.OPENROUTER
    llm_model: str = "anthropic/claude-sonnet-4.5"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    """Override the provider endpoint (any OpenAI-compatible API)."""
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=8192, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_reasoning_effort: ReasoningEffort | None = None
    """Reasoning models only; unset = provider default (not sent)."""
    llm_circuit_breaker_failure_threshold: int = Field(default=5, ge=1)
    """Consecutive LLM failures (after retries) that open the circuit."""
    llm_circuit_breaker_reset_seconds: float = Field(default=60.0, gt=0)
    """How long an open circuit fails fast before letting one trial call through."""

    generation_max_attempts: int = Field(default=2, ge=1)
    """Total generation attempts; >1 regenerates with the validation issues as feedback."""
    generation_retry_budget_seconds: float = Field(default=90.0, gt=0)
    """No retry starts once the run is older than this (bounds the synchronous request)."""

    ruff_timeout_seconds: float = Field(default=20.0, gt=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator(
        "llm_config_file", "llm_api_key", "llm_base_url", "llm_reasoning_effort", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Docker Compose passes unset variables as empty strings.
        return None if value == "" else value

    def llm_settings(self) -> LLMSettings:
        if self.llm_config_file is not None:
            return LLMSettings.from_yaml(self.llm_config_file, read_env=self.read_env)
        name = self.llm_provider.value
        return LLMSettings(
            providers={
                name: ProviderSettings(
                    type=self.llm_provider,
                    api_key=self.llm_api_key,
                    base_url=self.llm_base_url,
                    timeout_seconds=self.llm_timeout_seconds,
                    max_retries=self.llm_max_retries,
                    circuit_breaker=CircuitBreakerSettings(
                        failure_threshold=self.llm_circuit_breaker_failure_threshold,
                        reset_seconds=self.llm_circuit_breaker_reset_seconds,
                    ),
                )
            },
            routes=(
                Route(
                    provider=name,
                    model=self.llm_model,
                    reasoning_effort=self.llm_reasoning_effort,
                ),
            ),
        )

    def read_env(self, name: str) -> str | None:
        """A variable named by a config file (e.g. llm.yml api_key_env), read the way every
        setting is: process environment first, then the .env file."""
        if name in os.environ:
            return os.environ[name]
        env_file = self.model_config.get("env_file")
        if isinstance(env_file, str) and Path(env_file).is_file():
            return dotenv_values(env_file).get(name)
        return None
