"""The port features depend on: vendor-neutral text generation.

Features never see providers, models or routes: the composition root hands them the
LLMGateway (see gateway.py), which implements this port.
"""

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
    """Declared provider that answered (e.g. "openrouter")."""
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float
    finish_reason: str | None = None
    failed_routes: tuple[str, ...] = ()
    """Routes tried before this one, as "provider/model: reason" (empty = first route)."""


class LLM(Protocol):
    """Vendor-neutral text generation.

    Failures are app.shared.integrations.errors.IntegrationError (every route failed).
    """

    async def generate(self, request: LLMRequest) -> LLMResponse: ...
