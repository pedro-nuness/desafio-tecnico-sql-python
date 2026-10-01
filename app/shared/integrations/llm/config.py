from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import SecretStr

type ReasoningEffort = Literal["minimal", "low", "medium", "high"]


class LLMProviderName(StrEnum):
    OPENAI = "openai"
    OPENROUTER = "openrouter"


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Everything `create_llm_provider` needs; built from Settings by the caller."""

    provider: LLMProviderName
    model: str
    api_key: SecretStr | None = None
    base_url: str | None = None
    """Override the provider endpoint (any OpenAI-compatible API)."""
    app_name: str | None = None
    """Sent as attribution where the provider supports it (OpenRouter app title)."""
    timeout_seconds: float = 120.0
    max_retries: int = 2
    reasoning_effort: ReasoningEffort | None = None
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_reset_seconds: float = 60.0
