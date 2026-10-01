import logging
from uuid import UUID

from app.features.modernization.application.ports.pipeline.modernization_pipeline import (
    ModernizationPipeline,
)
from app.features.modernization.application.ports.repositories.unit_of_work import (
    UnitOfWorkFactory,
)
from app.features.modernization.domain.enums import ModernizationStatus
from app.features.modernization.domain.exceptions import ModernizationNotFoundError
from app.features.modernization.domain.models.modernization import (
    Modernization,
    PipelineError,
    PipelineProgress,
)

logger = logging.getLogger(__name__)


class ModernizationService:
    """Use cases: modernize one routine, and read a recorded execution.

    The graph records normal completion. Global HTTP handlers record interrupted runs.
    """

    def __init__(self, pipeline: ModernizationPipeline, uow_factory: UnitOfWorkFactory) -> None:
        self._pipeline = pipeline
        self._uow_factory = uow_factory

    async def modernize(
        self,
        source_code: str,
        schema_context: str | None = None,
        *,
        progress: PipelineProgress | None = None,
    ) -> Modernization:
        finished = await self._pipeline.run(
            source_code=source_code,
            schema_context=schema_context,
            progress=progress,
        )
        logger.info(
            "modernization finished",
            extra={"execution_id": str(finished.id), "status": finished.status.value},
        )
        return finished

    async def record_failure(
        self, progress: PipelineProgress, exc: Exception
    ) -> Modernization | None:
        """Called by the global handler before responding to a failed HTTP request."""
        if progress.execution_id is None:
            return None
        outcome = progress.outcome.model_copy(
            update={
                "errors": (
                    *progress.outcome.errors,
                    PipelineError(
                        step=progress.step,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    ),
                ),
            }
        )
        async with self._uow_factory() as uow:
            running = await uow.modernizations.find_by_id(progress.execution_id)
            if running is None:
                raise ModernizationNotFoundError(progress.execution_id)
            finished = running.complete(outcome).model_copy(
                update={"status": ModernizationStatus.FAILURE}
            )
            await uow.modernizations.update(finished)
            await uow.commit()
        return finished

    async def get(self, modernization_id: UUID) -> Modernization:
        async with self._uow_factory() as uow:
            modernization = await uow.modernizations.find_by_id(modernization_id)
        if modernization is None:
            raise ModernizationNotFoundError(modernization_id)
        return modernization
