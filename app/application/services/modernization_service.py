import logging
from uuid import UUID

from app.application.ports.pipeline.modernization_pipeline import ModernizationPipeline
from app.application.ports.repositories.unit_of_work import UnitOfWorkFactory
from app.domain.exceptions import ModernizationNotFoundError
from app.domain.models.modernization import Modernization

logger = logging.getLogger(__name__)


class ModernizationService:
    """Use case: modernize one routine and record the execution, whatever happens.

    Two short transactions instead of one long one: the RUNNING row is committed before
    the (slow, failure-prone) LLM call, so every execution leaves a trace even if the
    process dies; the final state is written once the pipeline returns.
    """

    def __init__(self, pipeline: ModernizationPipeline, uow_factory: UnitOfWorkFactory) -> None:
        self._pipeline = pipeline
        self._uow_factory = uow_factory

    async def modernize(self, source_code: str, schema_context: str | None = None) -> Modernization:
        modernization = Modernization.start(source_code, schema_context)
        async with self._uow_factory() as uow:
            await uow.modernizations.save(modernization)
            await uow.commit()

        outcome = await self._pipeline.run(
            execution_id=modernization.id,
            source_code=source_code,
            schema_context=schema_context,
        )
        finished = modernization.complete(outcome)

        async with self._uow_factory() as uow:
            await uow.modernizations.update(finished)
            await uow.commit()

        logger.info(
            "modernization finished",
            extra={"execution_id": str(finished.id), "status": finished.status.value},
        )
        return finished

    async def get(self, modernization_id: UUID) -> Modernization:
        async with self._uow_factory() as uow:
            modernization = await uow.modernizations.find_by_id(modernization_id)
        if modernization is None:
            raise ModernizationNotFoundError(modernization_id)
        return modernization
