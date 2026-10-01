"""LLMProvider adapter using the official OpenRouter Python SDK."""

import asyncio
import time

import httpx
from openrouter import OpenRouter, errors
from openrouter.types import UNSET

from app.shared.integrations.exceptions import IntegrationError
from app.shared.integrations.llm.config import ReasoningEffort
from app.shared.integrations.llm.llm_provider import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ResponseFormat,
)
from app.shared.resilience.circuit_breaker import CircuitBreaker

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
        max_retries: int = 2,
        reasoning_effort: ReasoningEffort | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._breaker = breaker
        self._max_retries = max_retries
        self._client = OpenRouter(
            api_key=api_key,
            server_url=base_url or OPENROUTER_BASE_URL,
            x_open_router_title=app_name,
            timeout_ms=int(timeout_seconds * 1000),
            # ponytail: SDK retries are time-based; use them when an attempt limit is supported.
            retry_config=None,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if self._breaker is not None:
            return await self._breaker.call(lambda: self._generate(request))
        return await self._generate(request)

    async def _generate(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        for attempt in range(self._max_retries + 1):
            try:
                completion = await self._client.chat.send_async(
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
            except errors.OpenRouterError as exc:
                retryable = exc.status_code in (408, 409, 429) or exc.status_code >= 500
                if not retryable or attempt == self._max_retries:
                    raise IntegrationError(f"openrouter request failed: {exc}") from exc
            except (httpx.TransportError, errors.NoResponseError) as exc:
                if attempt == self._max_retries:
                    raise IntegrationError(f"openrouter request failed: {exc}") from exc
            else:
                break
            await asyncio.sleep(0.5 * (2**attempt))

        latency_ms = (time.perf_counter() - started) * 1000
        if not completion.choices:
            raise IntegrationError("openrouter returned no choices")
        choice = completion.choices[0]
        content = choice.message.content
        if content is None or content == UNSET:
            content = ""
        if not isinstance(content, str):
            raise IntegrationError("openrouter returned non-text content")
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
