"""Provider using the official OpenRouter Python SDK.

The SDK only offers time-based retries, so they are disabled and the Integration retries
transient failures a bounded number of times (the provider's max_retries).
"""

import time

from openrouter import OpenRouter, components
from openrouter.types import UNSET

from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.integration import Integration
from app.shared.integrations.llm.config import Route
from app.shared.integrations.llm.llm import LLMRequest, LLMResponse, ResponseFormat

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    def __init__(
        self,
        name: str,
        *,
        api_key: str,
        app_name: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 120.0,
        integration: Integration | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self.name = name
        self._integration = integration or Integration(name)
        self._client = OpenRouter(
            api_key=api_key,
            server_url=base_url or OPENROUTER_BASE_URL,
            x_open_router_title=app_name,
            timeout_ms=int(timeout_seconds * 1000),
            retry_config=None,
        )

    async def complete(self, request: LLMRequest, route: Route) -> LLMResponse:
        started = time.perf_counter()

        async def send() -> components.ChatResult:
            completion = await self._client.chat.send_async(
                model=route.model,
                messages=[
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_prompt},
                ],
                temperature=request.temperature,
                max_completion_tokens=request.max_output_tokens,
                reasoning_effort=route.reasoning_effort or UNSET,
                response_format=(
                    {"type": "json_object"}
                    if request.response_format is ResponseFormat.JSON
                    else {"type": "text"}
                ),
                stream=False,
            )
            if completion.choices and completion.choices[0].finish_reason == "error":
                # OpenRouter answers 200 when the upstream provider fails mid-answer: the
                # content is cut off (observed: unterminated JSON). Transient, so retried.
                raise IntegrationError(
                    f"{self.name}: the upstream provider failed mid-answer",
                    transient=True,
                    integration=self.name,
                    model=route.model,
                )
            return completion

        completion = await self._integration.call(send)
        latency_ms = (time.perf_counter() - started) * 1000

        if not completion.choices:
            raise self._integration.error("returned no choices", model=route.model)
        choice = completion.choices[0]
        content = choice.message.content
        if content is None or content == UNSET:
            content = ""
        if not isinstance(content, str):
            raise self._integration.error("returned non-text content", model=route.model)
        usage = completion.usage
        return LLMResponse(
            content=content,
            provider=self.name,
            model=completion.model or route.model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=round(latency_ms, 2),
            finish_reason=choice.finish_reason,
        )
