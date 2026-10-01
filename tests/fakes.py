"""In-memory test doubles for persistence and LLM ports."""

import json
from collections.abc import Sequence
from uuid import UUID

from app.features.modernization.domain import Modernization
from app.features.modernization.evaluation.domain import Evaluation
from app.features.modernization.persistence.repository import not_found
from app.features.modernization.validation.checks.behavior.domain import CaseResult
from app.shared.integrations.llm.llm import LLMRequest, LLMResponse


class InMemoryDatabase:
    """Storage shared by the in-memory repositories."""

    def __init__(self) -> None:
        self.rows: dict[UUID, Modernization] = {}
        self.history: list[Modernization] = []
        """Every written version, in order (lets tests see RUNNING -> final)."""
        self.evaluations: list[Evaluation] = []


class InMemoryModernizationRepository:
    """ModernizationRepository port: every write is committed at once, like the SQL one."""

    def __init__(self, database: InMemoryDatabase) -> None:
        self._database = database

    async def save(self, modernization: Modernization) -> None:
        self._write(modernization)

    async def update(self, modernization: Modernization) -> None:
        if modernization.id not in self._database.rows:
            raise not_found(modernization.id)
        self._write(modernization)

    async def get(self, modernization_id: UUID) -> Modernization:
        found = self._database.rows.get(modernization_id)
        if found is None:
            raise not_found(modernization_id)
        return found

    def _write(self, modernization: Modernization) -> None:
        self._database.rows[modernization.id] = modernization
        self._database.history.append(modernization)


class InMemoryEvaluationRepository:
    """EvaluationRepository port."""

    def __init__(self, database: InMemoryDatabase) -> None:
        self._database = database

    async def save(self, evaluation: Evaluation) -> None:
        self._database.evaluations.append(evaluation)

    async def latest_per_procedure(self) -> tuple[Evaluation, ...]:
        latest: dict[str, Evaluation] = {}
        for evaluation in sorted(self._database.evaluations, key=lambda e: e.created_at):
            latest[evaluation.procedure_name] = evaluation
        return tuple(latest[name] for name in sorted(latest))


class FakeMetric:
    """EquivalenceMetric port: scripted case results, or raises `error`."""

    def __init__(self, cases: Sequence[CaseResult] = (), *, error: Exception | None = None) -> None:
        self._cases = tuple(cases)
        self._error = error
        self.evaluated: list[Modernization] = []

    async def evaluate(self, modernization: Modernization) -> tuple[CaseResult, ...]:
        self.evaluated.append(modernization)
        if self._error is not None:
            raise self._error
        return self._cases


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
