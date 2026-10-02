import json
from functools import partial
from unittest.mock import AsyncMock

import httpx
import pytest
from openrouter import OpenRouter
from openrouter.errors import OpenRouterError

from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.integration import Integration
from app.shared.integrations.llm.config import ReasoningEffort, Route
from app.shared.integrations.llm.llm import LLMRequest, ResponseFormat
from app.shared.integrations.llm.openrouter.provider import OpenRouterProvider
from app.shared.resilience.circuit_breaker import CircuitState

ROUTE = Route(provider="openrouter", model="model")


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
            "openrouter",
            api_key="sk-test",
            app_name="my-app",
            base_url="https://example.com/custom/v1/",
            timeout_seconds=1.25,
        )
        route = Route(
            provider="openrouter", model="requested/model", reasoning_effort=reasoning_effort
        )
        with provider._client:
            response = await provider.complete(
                LLMRequest(
                    system_prompt="system",
                    user_prompt="user",
                    temperature=0.25,
                    max_output_tokens=777,
                    response_format=response_format,
                ),
                route,
            )
    assert response.content == "ok"
    assert response.provider == "openrouter"
    assert response.model == "actual/model"
    assert (response.input_tokens, response.output_tokens, response.finish_reason) == (2, 1, "stop")
    assert response.latency_ms >= 0


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 401, "timeout", "network"])
async def test_integration_retries_and_counts_transient_sdk_failures(
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
    monkeypatch.setattr("app.shared.integrations.integration.asyncio.sleep", sleep)
    integration = Integration("openrouter", retries=2, failure_threshold=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(
            "app.shared.integrations.llm.openrouter.provider.OpenRouter",
            partial(OpenRouter, async_client=http),
        )
        provider = OpenRouterProvider("openrouter", api_key="sk-test", integration=integration)
        with provider._client:
            request = LLMRequest(system_prompt="s", user_prompt="u")
            with pytest.raises(IntegrationError) as exc_info:
                await provider.complete(request, ROUTE)
            assert isinstance(exc_info.value.__cause__, (OpenRouterError, httpx.TransportError))
            assert "provider down" not in exc_info.value.message
            assert exc_info.value.timeout is (status in (408, "timeout"))
            assert attempts == (3 if retryable else 1)
            assert sleep.await_count == (2 if retryable else 0)
            if not retryable:
                # A caller error (auth, bad request) says nothing about provider health.
                assert integration.breaker.state is CircuitState.CLOSED
                return
            assert integration.breaker._failures == 1
            assert integration.breaker.state is CircuitState.OPEN
            with pytest.raises(IntegrationError) as exc_info:
                await provider.complete(request, ROUTE)
            assert exc_info.value.retry_after is not None
            assert attempts == 3


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
        provider = OpenRouterProvider("openrouter", api_key="sk-test")
        with provider._client:
            request = LLMRequest(system_prompt="s", user_prompt="u")
            if isinstance(content, list):
                with pytest.raises(IntegrationError, match="non-text content"):
                    await provider.complete(request, ROUTE)
            else:
                response = await provider.complete(request, ROUTE)
                assert response.content == ""
                assert response.input_tokens is None and response.output_tokens is None
                assert response.finish_reason == "length"


async def test_an_upstream_failure_mid_answer_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter answers 200 with finish_reason "error" and a cut-off content (observed:
    unterminated JSON that failed generation); it must be retried, not parsed."""
    failed = _completion()
    failed["choices"][0]["finish_reason"] = "error"
    failed["choices"][0]["message"]["content"] = '{\n  "python_code": "from decimal'
    answers = iter([failed, _completion()])
    monkeypatch.setattr("app.shared.integrations.integration.asyncio.sleep", AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(answers)))
    ) as http:
        monkeypatch.setattr(
            "app.shared.integrations.llm.openrouter.provider.OpenRouter",
            partial(OpenRouter, async_client=http),
        )
        provider = OpenRouterProvider(
            "openrouter", api_key="sk-test", integration=Integration("openrouter", retries=1)
        )
        with provider._client:
            response = await provider.complete(
                LLMRequest(system_prompt="s", user_prompt="u"), ROUTE
            )

    assert (response.content, response.finish_reason) == ("ok", "stop")
