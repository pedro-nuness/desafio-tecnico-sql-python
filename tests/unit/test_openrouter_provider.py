import json
from functools import partial
from unittest.mock import AsyncMock

import httpx
import pytest
from openrouter import OpenRouter
from openrouter.errors import OpenRouterError

from app.shared.integrations.exceptions import IntegrationError
from app.shared.integrations.llm.config import ReasoningEffort
from app.shared.integrations.llm.llm_provider import LLMRequest, ResponseFormat
from app.shared.integrations.llm.openrouter.provider import OpenRouterProvider
from app.shared.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def _completion() -> dict:
    return {
        "id": "test",
        "created": 1,
        "model": "actual/model",
        "object": "chat.completion",
        "system_fingerprint": None,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


@pytest.mark.parametrize(
    ("response_format", "reasoning_effort"),
    [(ResponseFormat.TEXT, None), (ResponseFormat.JSON, "low")],
)
async def test_official_sdk_request_and_response_mapping(
    monkeypatch: pytest.MonkeyPatch,
    response_format: ResponseFormat,
    reasoning_effort: ReasoningEffort | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://example.com/custom/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer sk-test"
        assert request.headers["X-OpenRouter-Title"] == "my-app"
        assert request.extensions["timeout"]["read"] == 1.25
        body = json.loads(request.content)
        assert body["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ]
        assert body["model"] == "requested/model"
        assert body["temperature"] == 0.25
        assert body["max_completion_tokens"] == 777
        assert body["stream"] is False
        assert body["response_format"] == {
            "type": "json_object" if response_format is ResponseFormat.JSON else "text"
        }
        if reasoning_effort is None:
            assert "reasoning_effort" not in body
        else:
            assert body["reasoning_effort"] == reasoning_effort
        return httpx.Response(200, json=_completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            "app.shared.integrations.llm.openrouter.provider.OpenRouter",
            partial(OpenRouter, async_client=http),
        )
        provider = OpenRouterProvider(
            api_key="sk-test",
            model="requested/model",
            app_name="my-app",
            base_url="https://example.com/custom/v1/",
            timeout_seconds=1.25,
            reasoning_effort=reasoning_effort,
        )
        with provider._client:
            response = await provider.generate(
                LLMRequest(
                    system_prompt="system",
                    user_prompt="user",
                    temperature=0.25,
                    max_output_tokens=777,
                    response_format=response_format,
                )
            )
    assert response.content == "ok"
    assert response.provider == "openrouter"
    assert response.model == "actual/model"
    assert (response.input_tokens, response.output_tokens, response.finish_reason) == (2, 1, "stop")
    assert response.latency_ms >= 0


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 401, "timeout", "network"])
async def test_retry_limit_and_breaker_count_with_official_sdk(
    monkeypatch: pytest.MonkeyPatch, status: int | str
) -> None:
    attempts = 0
    retryable = status != 401

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if status == "timeout":
            raise httpx.ReadTimeout("provider down", request=request)
        if status == "network":
            raise httpx.ConnectError("provider down", request=request)
        return httpx.Response(status, json={"error": {"code": status, "message": "provider down"}})

    sleep = AsyncMock()
    monkeypatch.setattr("app.shared.integrations.llm.openrouter.provider.asyncio.sleep", sleep)
    breaker = CircuitBreaker("openrouter", failure_threshold=1, failure_types=(IntegrationError,))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            "app.shared.integrations.llm.openrouter.provider.OpenRouter",
            partial(OpenRouter, async_client=http),
        )
        provider = OpenRouterProvider(
            api_key="sk-test", model="model", max_retries=2, breaker=breaker
        )
        with provider._client:
            request = LLMRequest(system_prompt="s", user_prompt="u")
            with pytest.raises((OpenRouterError, httpx.TransportError), match="provider down"):
                await provider.generate(request)
            assert attempts == (3 if retryable else 1)
            assert sleep.await_count == (2 if retryable else 0)
            assert breaker._failures == 1
            assert breaker.state is CircuitState.OPEN
            with pytest.raises(CircuitOpenError):
                await provider.generate(request)
            assert attempts == (3 if retryable else 1)


@pytest.mark.parametrize("content", [None, "missing", []])
async def test_optional_response_fields_and_non_text_content(
    monkeypatch: pytest.MonkeyPatch, content: str | list | None
) -> None:
    payload = _completion()
    payload.pop("usage")
    payload["choices"][0]["finish_reason"] = "length"
    message = payload["choices"][0]["message"]
    if content == "missing":
        message.pop("content")
    else:
        message["content"] = content
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        monkeypatch.setattr(
            "app.shared.integrations.llm.openrouter.provider.OpenRouter",
            partial(OpenRouter, async_client=http),
        )
        provider = OpenRouterProvider(api_key="sk-test", model="model")
        with provider._client:
            request = LLMRequest(system_prompt="s", user_prompt="u")
            if isinstance(content, list):
                with pytest.raises(IntegrationError, match="non-text content"):
                    await provider.generate(request)
            else:
                response = await provider.generate(request)
                assert response.content == ""
                assert response.input_tokens is None and response.output_tokens is None
                assert response.finish_reason == "length"
