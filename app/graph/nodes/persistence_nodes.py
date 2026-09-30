"""First and last nodes of the graph: every run is recorded, whoever started it.

Persistence lives in the graph (not only in the HTTP use case) because the graph has more
than one entry point: POST /modernize, the LangGraph API (/runs) and Studio. Two short
transactions: the RUNNING row is committed before the slow, failure-prone LLM call, so a
run leaves a trace even if the process dies; the final state is written at the end.
Persistence errors are not caught: a run that cannot be recorded must fail loudly.
"""

from app.application.ports.repositories.unit_of_work import UnitOfWorkFactory
from app.domain.enums import ModernizationStatus
from app.domain.exceptions import ModernizationNotFoundError
from app.domain.models.modernization import Modernization
from app.graph.state import ModernizationState, StateUpdate, to_outcome


class RecordStartNode:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        modernization = Modernization.start(state["source_code"], state.get("schema_context"))
        async with self._uow_factory() as uow:
            await uow.modernizations.save(modernization)
            await uow.commit()
        return StateUpdate(
            execution_id=modernization.id,
            started_at=modernization.created_at,
            generation_attempts=0,
            status=ModernizationStatus.RUNNING,
        )


class RecordResultNode:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def __call__(self, state: ModernizationState) -> StateUpdate:
        execution_id = state["execution_id"]
        async with self._uow_factory() as uow:
            running = await uow.modernizations.find_by_id(execution_id)
            if running is None:
                raise ModernizationNotFoundError(execution_id)
            finished = running.complete(to_outcome(state))
            await uow.modernizations.update(finished)
            await uow.commit()
        return StateUpdate(modernization=finished, status=finished.status)
