import logging
from uuid import UUID

from app.features.modernization.application.ports.pipeline.modernization_pipeline import (
    ModernizationPipeline,
)
from app.features.modernization.application.ports.repositories.unit_of_work import (
    UnitOfWorkFactory,
)
from app.features.modernization.domain.exceptions import ModernizationNotFoundError
from app.features.modernization.domain.models.modernization import Modernization

logger = logging.getLogger(__name__)


class ModernizationService:
    """Use cases: modernize one routine, and read a recorded execution.

    Recording the run is part of the pipeline contract (the graph persists it), so the
    same guarantee holds for runs started outside this service (LangGraph API / Studio).
    """

    def __init__(self, pipeline: ModernizationPipeline, uow_factory: UnitOfWorkFactory) -> None:
        self._pipeline = pipeline
        self._uow_factory = uow_factory

    async def modernize(self, source_code: str, schema_context: str | None = None) -> Modernization:
        finished = await self._pipeline.run(source_code=source_code, schema_context=schema_context)
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
