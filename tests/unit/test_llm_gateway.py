"""LLMGateway: route order, failover, budget and the error when every route fails."""

import pytest

from app.shared.errors import AppError
from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.llm.config import Route
from app.shared.integrations.llm.gateway import LLMGateway
from app.shared.integrations.llm.llm import LLMRequest, LLMResponse

REQUEST = LLMRequest(system_prompt="s", user_prompt="u")


class FakeProvider:
    """Answers, or raises the scripted error for a given model."""

    def __init__(self, name: str, errors: dict[str, Exception] | None = None) -> None:
        self.name = name
        self._errors = errors or {}
        self.calls: list[str] = []

    async def complete(self, request: LLMRequest, route: Route) -> LLMResponse:
        self.calls.append(route.model)
        if route.model in self._errors:
            raise self._errors[route.model]
        return LLMResponse(content="ok", provider=self.name, model=route.model, latency_ms=1.0)


def _routes(*labels: str) -> tuple[Route, ...]:
    return tuple(Route(provider=label.split("/")[0], model=label.split("/")[1]) for label in labels)


def _gateway(*providers: FakeProvider, routes: tuple[Route, ...]) -> LLMGateway:
    return LLMGateway({p.name: p for p in providers}, routes)


async def test_first_healthy_route_answers() -> None:
    a, b = FakeProvider("a"), FakeProvider("b")

    response = await _gateway(a, b, routes=_routes("a/m1", "b/m2")).generate(REQUEST)

    assert (response.provider, response.model, response.failed_routes) == ("a", "m1", ())
    assert b.calls == []


async def test_failing_routes_hand_over_in_order_and_are_reported() -> None:
    a = FakeProvider("a", {"m1": IntegrationError("a timed out", timeout=True)})
    b = FakeProvider("b", {"m2": IntegrationError("b answered HTTP 400")})
    c = FakeProvider("c")

    response = await _gateway(a, b, c, routes=_routes("a/m1", "b/m2", "c/m3")).generate(REQUEST)

    assert (response.provider, response.model) == ("c", "m3")
    assert response.failed_routes == ("a/m1: a timed out", "b/m2: b answered HTTP 400")


async def test_bugs_propagate_without_trying_other_routes() -> None:
    a, b = FakeProvider("a", {"m1": TypeError("bug")}), FakeProvider("b")

    with pytest.raises(TypeError):
        await _gateway(a, b, routes=_routes("a/m1", "b/m2")).generate(REQUEST)

    assert b.calls == []


async def test_all_routes_failing_raise_one_integration_error_with_the_attempts() -> None:
    a = FakeProvider("a", {"m1": IntegrationError("a is unavailable", retry_after=40)})
    b = FakeProvider("b", {"m2": IntegrationError("b is unavailable", retry_after=12)})

    with pytest.raises(IntegrationError, match="every route failed") as exc_info:
        await _gateway(a, b, routes=_routes("a/m1", "b/m2")).generate(REQUEST)

    error = exc_info.value
    assert error.payload["attempts"] == [
        {"route": "a/m1", "error": "a is unavailable"},
        {"route": "b/m2", "error": "b is unavailable"},
    ]
    assert error.payload["skipped_routes"] == []
    assert error.retry_after == 12  # every circuit open: the earliest one may close first


async def test_retry_after_only_when_every_route_is_waiting_on_a_circuit() -> None:
    a = FakeProvider("a", {"m1": IntegrationError("a is unavailable", retry_after=40)})
    b = FakeProvider("b", {"m2": IntegrationError("b answered HTTP 500")})

    with pytest.raises(IntegrationError) as exc_info:
        await _gateway(a, b, routes=_routes("a/m1", "b/m2")).generate(REQUEST)

    assert exc_info.value.retry_after is None
    assert not exc_info.value.timeout


async def test_budget_stops_starting_new_routes() -> None:
    now = [0.0]

    class SlowFailure(FakeProvider):
        async def complete(self, request: LLMRequest, route: Route) -> LLMResponse:
            now[0] += 100
            return await super().complete(request, route)

    a = SlowFailure("a", {"m1": IntegrationError("a timed out", timeout=True)})
    b = FakeProvider("b")
    gateway = LLMGateway(
        {"a": a, "b": b}, _routes("a/m1", "b/m2"), budget_seconds=60, clock=lambda: now[0]
    )

    with pytest.raises(IntegrationError, match=r"time budget of 60s exhausted") as exc_info:
        await gateway.generate(REQUEST)

    assert exc_info.value.payload["skipped_routes"] == ["b/m2"]
    assert exc_info.value.timeout
    assert b.calls == []


def test_routes_must_reference_built_providers() -> None:
    with pytest.raises(AppError, match="unknown providers \\['b'\\]"):
        _gateway(FakeProvider("a"), routes=_routes("a/m1", "b/m2"))


def test_at_least_one_route_is_required() -> None:
    with pytest.raises(AppError, match="at least one route"):
        _gateway(FakeProvider("a"), routes=())
