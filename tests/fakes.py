"""In-memory test doubles for persistence and LLM ports."""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

from app.features.modernization.domain.modernization import Modernization
from app.features.modernization.persistence.repository import not_found
from app.shared.integrations.llm.llm import LLMRequest, LLMResponse


class InMemoryTransaction:
    """Buffers writes; only commit() publishes them to the database."""

    def __init__(self, database: InMemoryDatabase) -> None:
        self._database = database
        self.pending: dict[UUID, Modernization] = {}

    async def commit(self) -> None:
        self._database.rows.update(self.pending)
        self._database.history.extend(self.pending.values())
        self.pending.clear()

    async def rollback(self) -> None:
        self.pending.clear()


class InMemoryDatabase:
    """Fake TransactionManager + storage: committed rows and every committed version."""

    def __init__(self) -> None:
        self.rows: dict[UUID, Modernization] = {}
        self.history: list[Modernization] = []
        """Every committed version, in order (lets tests see RUNNING -> final)."""
        self._active: InMemoryTransaction | None = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[InMemoryTransaction]:
        if self._active is not None:
            raise RuntimeError("Nested transactions are not supported")
        self._active = InMemoryTransaction(self)
        try:
            yield self._active
        finally:
            await self._active.rollback()  # leaving without commit() discards the writes
            self._active = None

    def current(self) -> InMemoryTransaction:
        if self._active is None:
            raise RuntimeError("Repository used outside a transaction")
        return self._active


class InMemoryModernizationRepository:
    """ModernizationRepository port, same contract as the SQLAlchemy one: current transaction."""

    def __init__(self, database: InMemoryDatabase) -> None:
        self._database = database

    async def save(self, modernization: Modernization) -> None:
        self._database.current().pending[modernization.id] = modernization

    async def update(self, modernization: Modernization) -> None:
        pending = self._database.current().pending
        if modernization.id not in self._database.rows and modernization.id not in pending:
            raise not_found(modernization.id)
        pending[modernization.id] = modernization

    async def get(self, modernization_id: UUID) -> Modernization:
        pending = self._database.current().pending
        found = pending.get(modernization_id) or self._database.rows.get(modernization_id)
        if found is None:
            raise not_found(modernization_id)
        return found


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


class FakeLLM:
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
            raise ValueError("FakeLLM needs at least one response")
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
