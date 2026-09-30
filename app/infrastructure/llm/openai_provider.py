"""LLMProvider adapter for the OpenAI Chat Completions API.

Also serves any OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama...) through
`base_url`; `provider_name` is what ends up in the report.
"""

import time
from collections.abc import Mapping

from openai import APIError, AsyncOpenAI, omit
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared import ReasoningEffort

from app.application.ports.llm.llm_provider import LLMRequest, LLMResponse, ResponseFormat
from app.domain.exceptions import LLMProviderError


class OpenAIProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        provider_name: str = "openai",
        base_url: str | None = None,
        default_headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> None:
        self._model = model
        self._provider_name = provider_name
        self._reasoning_effort = reasoning_effort
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ]
        started = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=request.temperature,
                max_completion_tokens=request.max_output_tokens,
                # Reasoning tokens count against max_completion_tokens: an unbounded
                # reasoning model can spend the whole budget and return no content.
                reasoning_effort=self._reasoning_effort or omit,
                response_format=(
                    {"type": "json_object"}
                    if request.response_format is ResponseFormat.JSON
                    else {"type": "text"}
                ),
            )
        except APIError as exc:
            raise LLMProviderError(f"{self._provider_name} request failed: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000

        if not completion.choices:
            raise LLMProviderError(f"{self._provider_name} returned no choices")
        choice = completion.choices[0]
        usage = completion.usage
        return LLMResponse(
            content=choice.message.content or "",
            provider=self._provider_name,
            model=completion.model or self._model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=round(latency_ms, 2),
            finish_reason=choice.finish_reason,
        )
