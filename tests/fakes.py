"""In-memory test doubles for persistence and LLM ports."""

import json
from collections.abc import Sequence
from types import TracebackType
from typing import Self
from uuid import UUID

from app.features.modernization.domain.models.modernization import Modernization
from app.shared.errors import NotFoundError
from app.shared.integrations.llm.llm_provider import LLMRequest, LLMResponse


class InMemoryModernizationRepository:
    def __init__(self, committed: dict[UUID, Modernization]) -> None:
        self._committed = committed
        self.pending: dict[UUID, Modernization] = {}

    async def save(self, modernization: Modernization) -> None:
        self.pending[modernization.id] = modernization

    async def update(self, modernization: Modernization) -> None:
        if modernization.id not in self._committed and modernization.id not in self.pending:
            raise NotFoundError(f"Modernization {modernization.id} not found")
        self.pending[modernization.id] = modernization

    async def find_by_id(self, modernization_id: UUID) -> Modernization | None:
        return self.pending.get(modernization_id) or self._committed.get(modernization_id)


class InMemoryUnitOfWork:
    """Mimics transactional semantics: only commit() publishes pending writes."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self.modernizations = InMemoryModernizationRepository(store.rows)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.rollback()

    async def commit(self) -> None:
        self._store.rows.update(self.modernizations.pending)
        self._store.history.extend(self.modernizations.pending.values())
        self.modernizations.pending.clear()

    async def rollback(self) -> None:
        self.modernizations.pending.clear()


class InMemoryStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, Modernization] = {}
        self.history: list[Modernization] = []
        """Every committed version, in order (lets tests see RUNNING -> final)."""

    def uow(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self)


DEFAULT_TEST_CODE = """\
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def modernized_routine(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT 1"))
"""

DEFAULT_TEST_RESPONSE = json.dumps(
    {
        "python_code": DEFAULT_TEST_CODE,
        "strategy": "hybrid",
        "architectural_decisions": [
            {
                "topic": "sql",
                "decision": "Test double output",
                "rationale": "In-memory test double for unit tests.",
            }
        ],
        "warnings": [],
    }
)


class FakeLLMProvider:
    """Returns scripted responses in order (the last one repeats) or raises `error`.

    Every request is recorded in `requests` so tests can assert on the prompt.
    """

    def __init__(
        self,
        responses: Sequence[str] = (DEFAULT_TEST_RESPONSE,),
        *,
        error: Exception | None = None,
        model: str = "test-model",
        provider: str = "fake",
    ) -> None:
        if not responses:
            raise ValueError("FakeLLMProvider needs at least one response")
        self._responses = list(responses)
        self._error = error
        self._model = model
        self._provider = provider
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        index = min(len(self.requests), len(self._responses)) - 1
        content = self._responses[index]
        return LLMResponse(
            content=content,
            provider=self._provider,
            model=self._model,
            input_tokens=len(request.system_prompt + request.user_prompt) // 4,
            output_tokens=len(content) // 4,
            latency_ms=0.0,
            finish_reason="stop",
        )
