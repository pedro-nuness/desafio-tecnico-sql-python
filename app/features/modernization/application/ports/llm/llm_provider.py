from enum import StrEnum
from typing import Protocol

from pydantic import Field

from app.shared.domain.value_object import ValueObject


class ResponseFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class LLMRequest(ValueObject):
    system_prompt: str
    user_prompt: str
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4096, gt=0)
    response_format: ResponseFormat = ResponseFormat.TEXT


class LLMResponse(ValueObject):
    content: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float
    finish_reason: str | None = None


class LLMProvider(Protocol):
    """Vendor-neutral text generation.

    Adapters must raise app.features.modernization.domain.exceptions.LLMProviderError
    for any vendor failure, so callers never see SDK-specific exceptions.
    """

    async def generate(self, request: LLMRequest) -> LLMResponse: ...
