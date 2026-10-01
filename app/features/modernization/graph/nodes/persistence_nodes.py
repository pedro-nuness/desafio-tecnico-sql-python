"""Run persistence: start, normal completion, and interruption by an exception.

Persistence lives in the graph (not only in the HTTP use case) because the graph has more
than one entry point: POST /modernize, the LangGraph API (/runs) and Studio. Two short
transactions: the RUNNING row is committed before the slow, failure-prone LLM call, so a
run leaves a trace even if the process dies; the final state is written at the end.
Persistence errors are not caught: a run that cannot be recorded must fail loudly.
"""

from app.features.modernization.application.ports.repositories.modernization_repository import (
    ModernizationRepository,
)
from app.features.modernization.domain.enums import ModernizationStatus, PipelineStep
from app.features.modernization.domain.models.modernization import Modernization, PipelineError
from app.features.modernization.graph.state import (
    ModernizationState,
    StateUpdate,
    to_outcome,
)
from app.shared.errors import AppError, NotFoundError
from app.shared.persistence import TransactionManager


class RecordStartNode:
    def __init__(
        self, transactions: TransactionManager, modernizations: ModernizationRepository
    ) -> None:
        self._transactions = transactions
        self._modernizations = modernizations

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        modernization = Modernization.start(state["source_code"], state.get("schema_context"))
        async with self._transactions.transaction() as tx:
            await self._modernizations.save(modernization)
            await tx.commit()
        return StateUpdate(
            execution_id=modernization.id,
            started_at=modernization.created_at,
            generation_attempts=0,
            status=ModernizationStatus.RUNNING,
        )


class RecordResultNode:
    def __init__(
        self, transactions: TransactionManager, modernizations: ModernizationRepository
    ) -> None:
        self._transactions = transactions
        self._modernizations = modernizations

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        execution_id = state["execution_id"]
        async with self._transactions.transaction() as tx:
            running = await self._modernizations.find_by_id(execution_id)
            if running is None:
                raise NotFoundError(
                    f"Modernization {execution_id} not found", execution_id=str(execution_id)
                )
            finished = running.complete(to_outcome(state))
            await self._modernizations.update(finished)
            await tx.commit()
        return StateUpdate(modernization=finished, status=finished.status)


class RecordFailure:
    """Persists a run interrupted by an exception in `step`; the caller re-raises.

    Lives in the graph so runs started from the LangGraph API / Studio are recorded too,
    not only those coming through the FastAPI handlers.
    """

    def __init__(
        self, transactions: TransactionManager, modernizations: ModernizationRepository
    ) -> None:
        self._transactions = transactions
        self._modernizations = modernizations

    async def __call__(self, state: ModernizationState, step: PipelineStep, exc: Exception) -> None:
        execution_id = state["execution_id"]
        outcome = to_outcome(state)
        error = PipelineError(
            step=step,
            error_type=type(exc).__name__,
            message=str(exc),
            payload=exc.payload if isinstance(exc, AppError) else {},
        )
        outcome = outcome.model_copy(update={"errors": (*outcome.errors, error)})
        async with self._transactions.transaction() as tx:
            running = await self._modernizations.find_by_id(execution_id)
            if running is None:
                raise NotFoundError(
                    f"Modernization {execution_id} not found", execution_id=str(execution_id)
                )
            finished = running.complete(outcome).model_copy(
                update={"status": ModernizationStatus.FAILURE}
            )
            await self._modernizations.update(finished)
            await tx.commit()
