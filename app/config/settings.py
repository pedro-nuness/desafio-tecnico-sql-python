from enum import StrEnum
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProviderName(StrEnum):
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    FAKE = "fake"


class Settings(BaseSettings):
    """Single source of configuration (environment variables / .env)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "plpgsql-modernizer"
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://modernizer:modernizer@localhost:5432/modernizer"
    )
    database_echo: bool = False

    llm_provider: LLMProviderName = LLMProviderName.FAKE
    llm_model: str = "fake-model"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    """Override the provider endpoint (any OpenAI-compatible API)."""
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=8192, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    """Reasoning models only; unset = provider default (not sent)."""

    generation_max_attempts: int = Field(default=2, ge=1)
    """Total generation attempts; >1 regenerates with the validation issues as feedback."""
    generation_retry_budget_seconds: float = Field(default=90.0, gt=0)
    """No retry starts once the run is older than this (bounds the synchronous request)."""

    ruff_timeout_seconds: float = Field(default=20.0, gt=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("llm_api_key", "llm_base_url", "llm_reasoning_effort", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Docker Compose passes unset variables as empty strings.
        return None if value == "" else value
