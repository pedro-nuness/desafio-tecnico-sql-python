"""LLMGateway: the central orchestrator between features and declared providers.

It is the `LLM` features receive. Generating walks the configured routes in priority order:
a route that fails with an IntegrationError (provider down, open circuit, timeout, rejected
request...) hands over to the next one, within the time budget. Anything else is a bug and
propagates untouched; an answer outside the caller's contract is the caller's business
(e.g. the repair loop).
"""

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from app.shared.errors import AppError
from app.shared.integrations.errors import IntegrationError
from app.shared.integrations.llm.config import Route
from app.shared.integrations.llm.llm import LLMRequest, LLMResponse


class Provider(Protocol):
    """SPI of one declared endpoint; only the gateway talks to providers."""

    name: str

    async def complete(self, request: LLMRequest, route: Route) -> LLMResponse: ...


class LLMGateway:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        routes: Sequence[Route],
        *,
        budget_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not routes:
            raise AppError("LLMGateway needs at least one route")
        missing = {route.provider for route in routes} - providers.keys()
        if missing:
            raise AppError(f"LLM routes use unknown providers {sorted(missing)}")
        self._providers = providers
        self._routes = tuple(routes)
        self._budget_seconds = budget_seconds
        self._clock = clock

    async def generate(self, request: LLMRequest) -> LLMResponse:
        deadline = (
            self._clock() + self._budget_seconds if self._budget_seconds is not None else None
        )
        failures: list[tuple[Route, IntegrationError]] = []
        for route in self._routes:
            if failures and deadline is not None and self._clock() >= deadline:
                break
            # Needed: failover is exactly "observe this route's failure, try the next".
            try:
                response = await self._providers[route.provider].complete(request, route)
            except IntegrationError as exc:
                failures.append((route, exc))
                continue
            return response.model_copy(
                update={"failed_routes": tuple(f"{r.label}: {e.message}" for r, e in failures)}
            )
        raise self._all_failed(failures)

    def _all_failed(self, failures: list[tuple[Route, IntegrationError]]) -> IntegrationError:
        errors = [error for _, error in failures]
        skipped = [route.label for route in self._routes[len(failures) :]]
        retry_afters = [e.retry_after for e in errors if e.retry_after is not None]
        reason = (
            f"time budget of {self._budget_seconds}s exhausted" if skipped else "every route failed"
        )
        return IntegrationError(
            f"LLM: {reason} ({len(failures)} of {len(self._routes)} tried)",
            # Every circuit open: the client can come back when the first one may close.
            retry_after=min(retry_afters) if len(retry_afters) == len(errors) else None,
            timeout=all(e.timeout for e in errors),
            attempts=[{"route": r.label, "error": e.message} for r, e in failures],
            skipped_routes=skipped,
        )
