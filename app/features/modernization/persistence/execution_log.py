"""Lifecycle of a run in the history table: started, completed, or interrupted.

Used by the graph (not only by the HTTP use case) because the graph has more than one entry
point: POST /modernize, the LangGraph API (/runs) and Studio. One short transaction per
transition: the RUNNING row is committed before the slow, failure-prone LLM call, so a run
leaves a trace even if the process dies. Persistence errors are not caught: a run that cannot
be recorded must fail loudly.
"""

from collections.abc import Callable
from uuid import UUID

from app.features.modernization.domain.modernization import (
    Modernization,
    PipelineError,
    PipelineOutcome,
)
from app.features.modernization.persistence.repository import ModernizationRepository
from app.shared.persistence import TransactionManager


class ExecutionLog:
    def __init__(
        self, transactions: TransactionManager, modernizations: ModernizationRepository
    ) -> None:
        self._transactions = transactions
        self._modernizations = modernizations

    async def start(self, source_code: str, schema_context: str | None) -> Modernization:
        modernization = Modernization.start(source_code, schema_context)
        async with self._transactions.transaction() as tx:
            await self._modernizations.save(modernization)
            await tx.commit()
        return modernization

    async def complete(self, execution_id: UUID, outcome: PipelineOutcome) -> Modernization:
        return await self._finish(execution_id, lambda running: running.complete(outcome))

    async def fail(
        self, execution_id: UUID, outcome: PipelineOutcome, error: PipelineError
    ) -> Modernization:
        return await self._finish(execution_id, lambda running: running.fail(outcome, error))

    async def _finish(
        self, execution_id: UUID, finish: Callable[[Modernization], Modernization]
    ) -> Modernization:
        async with self._transactions.transaction() as tx:
            finished = finish(await self._modernizations.get(execution_id))
            await self._modernizations.update(finished)
            await tx.commit()
        return finished
