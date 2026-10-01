"""LLMProvider adapter using the official OpenRouter Python SDK.

The SDK only offers time-based retries, so they are disabled and the Integration retries
transient failures a bounded number of times (LLM_MAX_RETRIES).
"""

import time

from openrouter import OpenRouter
from openrouter.types import UNSET

from app.shared.integrations.integration import Integration
from app.shared.integrations.llm.config import ReasoningEffort
from app.shared.integrations.llm.llm_provider import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ResponseFormat,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        app_name: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        reasoning_effort: ReasoningEffort | None = None,
        integration: Integration | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._integration = integration or Integration("openrouter")
        self._client = OpenRouter(
            api_key=api_key,
            server_url=base_url or OPENROUTER_BASE_URL,
            x_open_router_title=app_name,
            timeout_ms=int(timeout_seconds * 1000),
            retry_config=None,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        completion = await self._integration.call(
            lambda: self._client.chat.send_async(
                model=self._model,
                messages=[
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                temperature=request.temperature,
                max_completion_tokens=request.max_output_tokens,
                reasoning_effort=self._reasoning_effort or UNSET,
                response_format=(
                    {"type": "json_object"}
                    if request.response_format is ResponseFormat.JSON
                    else {"type": "text"}
                ),
                stream=False,
            )
        )
        latency_ms = (time.perf_counter() - started) * 1000

        if not completion.choices:
            raise self._integration.error("returned no choices")
        choice = completion.choices[0]
        content = choice.message.content
        if content is None or content == UNSET:
            content = ""
        if not isinstance(content, str):
            raise self._integration.error("returned non-text content")
        usage = completion.usage
        return LLMResponse(
            content=content,
            provider="openrouter",
            model=completion.model or self._model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=round(latency_ms, 2),
            finish_reason=choice.finish_reason,
        )
