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

ENV_FILE = ".env"
"""The only place values come from (copied from .env.example); environment variables override
it. No value is defined in code."""


class Settings(BaseSettings):
    """Single source of configuration: .env (or environment variables).

    No field has a default: a variable missing from .env fails the boot by name.
    """

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_name: str
    database_url: PostgresDsn
    database_echo: bool

    llm_config_file: Path | None
    """YAML with the declared providers and the routes tried in order
    (config/llm.example.yml). When unset, the LLM_* variables below describe one route."""
    llm_provider: ProviderType
    llm_model: str
    llm_api_key: SecretStr | None
    llm_base_url: str | None
    """Override the provider endpoint (any OpenAI-compatible API)."""
    llm_temperature: float = Field(ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(gt=0)
    llm_timeout_seconds: float = Field(gt=0)
    llm_max_retries: int = Field(ge=0)
    llm_reasoning_effort: ReasoningEffort | None
    """Reasoning models only; unset = provider default (not sent)."""
    llm_circuit_breaker_failure_threshold: int = Field(ge=1)
    """Consecutive LLM failures (after retries) that open the circuit."""
    llm_circuit_breaker_reset_seconds: float = Field(gt=0)
    """How long an open circuit fails fast before letting one trial call through."""

    code_generation_max_attempts: int = Field(ge=1)
    """Total generation attempts; >1 regenerates with the validation issues as feedback."""
    code_generation_retry_budget_seconds: float = Field(gt=0)
    """No retry starts once the run is older than this (bounds the synchronous request)."""

    ruff_timeout_seconds: float = Field(gt=0)

    langfuse_public_key: str | None
    langfuse_secret_key: SecretStr | None
    langfuse_base_url: str

    evaluation_database_url: PostgresDsn | None
    """Disposable database where the evaluation executes generated code (never the app's).
    Unset: the evaluation endpoints answer with an error; everything else works."""
    evaluation_dataset_file: Path
    evaluation_case_timeout_seconds: float = Field(gt=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    @field_validator(
        "llm_config_file",
        "llm_api_key",
        "llm_base_url",
        "llm_reasoning_effort",
        "evaluation_database_url",
        "langfuse_public_key",
        "langfuse_secret_key",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Optional values are written empty in .env.example (and Docker Compose passes unset
        # variables as empty strings): empty means "not set".
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
        setting is: process environment first, then .env."""
        if name in os.environ:
            return os.environ[name]
        if Path(ENV_FILE).is_file():
            return dotenv_values(ENV_FILE).get(name)
        return None
