"""Provider for the OpenAI Chat Completions API.

Also serves any OpenAI-compatible endpoint (vLLM, Ollama...) through `base_url`. Retries
are left to the SDK, so the Integration only translates errors and runs the breaker.
"""

import time
from collections.abc import Mapping

from openai import AsyncOpenAI, omit
from openai.types.chat import ChatCompletionMessageParam

from app.shared.integrations.integration import Integration
from app.shared.integrations.llm.config import Route
from app.shared.integrations.llm.llm import LLMRequest, LLMResponse, ResponseFormat


class OpenAIProvider:
    def __init__(
        self,
        name: str,
        *,
        api_key: str,
        base_url: str | None = None,
        default_headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 2,
        integration: Integration | None = None,
    ) -> None:
        self.name = name
        self._integration = integration or Integration(name)
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    async def complete(self, request: LLMRequest, route: Route) -> LLMResponse:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ]
        started = time.perf_counter()
        completion = await self._integration.call(
            lambda: self._client.chat.completions.create(
                model=route.model,
                messages=messages,
                temperature=request.temperature,
                max_completion_tokens=request.max_output_tokens,
                # Reasoning tokens count against max_completion_tokens: an unbounded
                # reasoning model can spend the whole budget and return no content.
                reasoning_effort=route.reasoning_effort or omit,
                response_format=(
                    {"type": "json_object"}
                    if request.response_format is ResponseFormat.JSON
                    else {"type": "text"}
                ),
            )
        )
        latency_ms = (time.perf_counter() - started) * 1000

        if not completion.choices:
            raise self._integration.error("returned no choices", model=route.model)
        choice = completion.choices[0]
        usage = completion.usage
        return LLMResponse(
            content=choice.message.content or "",
            provider=self.name,
            model=completion.model or route.model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=round(latency_ms, 2),
            finish_reason=choice.finish_reason,
        )
